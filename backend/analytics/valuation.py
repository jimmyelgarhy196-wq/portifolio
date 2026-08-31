"""Valuation engine: DCF, multiples-based fair value, and a blended view.

Two rules shape this module:

1. **A valuation is an opinion built on assumptions, and the assumptions are
   always shown.** Every result carries the exact inputs used, so a user can
   disagree with the growth rate rather than with an unexplained number.
2. **No input is invented.** If free cash flow, share count or the data needed
   for a discount rate is missing, the model reports itself as unavailable
   instead of substituting a default and presenting the output as a valuation.

Nothing here is advice or a price target. It is a calculator whose arithmetic
the user can check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from backend.core.data_quality import UNAVAILABLE, is_available, safe_div

#: Defaults reflect Egyptian market conditions and are *shown* to the user, who
#: can override every one of them. They are starting points, not truths.
DEFAULT_RISK_FREE = 0.22          # EGP treasury yields have been high; user-editable.
DEFAULT_EQUITY_RISK_PREMIUM = 0.07
DEFAULT_TERMINAL_GROWTH = 0.05    # Below expected long-run nominal EGP GDP growth.
DEFAULT_BETA = 1.0
DEFAULT_FORECAST_YEARS = 5

ASSUMPTION_NOTE = (
    "Every figure below is the output of assumptions you can change. A discounted "
    "cash flow model is highly sensitive to the discount rate and the terminal "
    "growth rate; small changes to either move the result substantially. This is a "
    "calculator, not a price target, and not investment advice."
)


@dataclass
class DcfAssumptions:
    """Everything the model needs, in one auditable object."""

    base_fcf: float | None = None
    shares_outstanding: float | None = None
    net_debt: float = 0.0
    growth_rate: float = 0.10
    fade_to: float | None = None
    terminal_growth: float = DEFAULT_TERMINAL_GROWTH
    discount_rate: float | None = None
    risk_free_rate: float = DEFAULT_RISK_FREE
    equity_risk_premium: float = DEFAULT_EQUITY_RISK_PREMIUM
    beta: float = DEFAULT_BETA
    years: int = DEFAULT_FORECAST_YEARS
    currency: str = "EGP"

    def cost_of_equity(self) -> float:
        """CAPM. Used when no discount rate is supplied directly."""
        return self.risk_free_rate + self.beta * self.equity_risk_premium

    def effective_discount_rate(self) -> float:
        return self.discount_rate if self.discount_rate is not None else self.cost_of_equity()

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_fcf": self.base_fcf, "shares_outstanding": self.shares_outstanding,
            "net_debt": self.net_debt, "growth_rate": self.growth_rate,
            "fade_to": self.fade_to, "terminal_growth": self.terminal_growth,
            "discount_rate": self.effective_discount_rate(),
            "discount_rate_source": "supplied" if self.discount_rate is not None else "CAPM",
            "risk_free_rate": self.risk_free_rate,
            "equity_risk_premium": self.equity_risk_premium, "beta": self.beta,
            "years": self.years, "currency": self.currency,
        }


@dataclass
class DcfResult:
    available: bool
    fair_value_per_share: float | None = None
    equity_value: float | None = None
    enterprise_value: float | None = None
    terminal_value: float | None = None
    terminal_share_of_value: float | None = None
    upside: float | None = None
    projections: list[dict[str, Any]] = field(default_factory=list)
    assumptions: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "fair_value_per_share": self.fair_value_per_share,
            "equity_value": self.equity_value,
            "enterprise_value": self.enterprise_value,
            "terminal_value": self.terminal_value,
            "terminal_share_of_value": self.terminal_share_of_value,
            "upside": self.upside,
            "projections": self.projections,
            "assumptions": self.assumptions,
            "notes": self.notes,
            "unavailable_reason": self.unavailable_reason,
        }


def run_dcf(assumptions: DcfAssumptions, *, current_price: float | None = None) -> DcfResult:
    """A two-stage FCF model: an explicit forecast, then a Gordon terminal value.

    Refuses to produce a number when an input is missing or when the arithmetic
    would be meaningless (terminal growth at or above the discount rate makes
    the terminal value infinite or negative).
    """
    notes: list[str] = []

    if not is_available(assumptions.base_fcf) or assumptions.base_fcf is None:
        return DcfResult(available=False, assumptions=assumptions.to_dict(),
                         unavailable_reason=(
                             "N/A — data unavailable. No free cash flow figure is stored "
                             "for this company, so a DCF cannot be built."))
    if not assumptions.shares_outstanding:
        return DcfResult(available=False, assumptions=assumptions.to_dict(),
                         unavailable_reason=(
                             "N/A — data unavailable. The share count is unknown, so a "
                             "per-share value cannot be derived."))
    if assumptions.base_fcf <= 0:
        return DcfResult(available=False, assumptions=assumptions.to_dict(),
                         unavailable_reason=(
                             "Free cash flow is negative or zero. A growth DCF is not a "
                             "meaningful model for a company that is not generating cash; "
                             "use the multiples view instead."))

    rate = assumptions.effective_discount_rate()
    if rate <= 0:
        return DcfResult(available=False, assumptions=assumptions.to_dict(),
                         unavailable_reason="The discount rate must be greater than zero.")
    if assumptions.terminal_growth >= rate:
        return DcfResult(available=False, assumptions=assumptions.to_dict(),
                         unavailable_reason=(
                             f"Terminal growth ({assumptions.terminal_growth:.1%}) is not "
                             f"below the discount rate ({rate:.1%}). The model would return "
                             "an infinite value, so no figure is produced."))

    years = max(1, min(int(assumptions.years), 15))
    start_growth = assumptions.growth_rate
    end_growth = (assumptions.fade_to if assumptions.fade_to is not None
                  else assumptions.terminal_growth)

    projections: list[dict[str, Any]] = []
    fcf = float(assumptions.base_fcf)
    pv_sum = 0.0
    for year in range(1, years + 1):
        # Growth fades linearly from the starting rate to the terminal rate, so
        # the model does not assume today's growth continues forever.
        weight = (year - 1) / max(years - 1, 1)
        growth = start_growth + (end_growth - start_growth) * weight
        fcf = fcf * (1 + growth)
        discount = (1 + rate) ** year
        pv = fcf / discount
        pv_sum += pv
        projections.append({
            "year": year, "growth": growth, "fcf": fcf,
            "discount_factor": 1 / discount, "present_value": pv,
        })

    terminal_fcf = fcf * (1 + assumptions.terminal_growth)
    terminal_value = terminal_fcf / (rate - assumptions.terminal_growth)
    pv_terminal = terminal_value / ((1 + rate) ** years)
    enterprise_value = pv_sum + pv_terminal
    equity_value = enterprise_value - (assumptions.net_debt or 0.0)
    per_share = equity_value / assumptions.shares_outstanding

    terminal_share = pv_terminal / enterprise_value if enterprise_value else None
    if terminal_share is not None and terminal_share > 0.75:
        notes.append(
            f"{terminal_share:.0%} of the value comes from the terminal value — the part "
            "of the model resting on assumptions beyond the forecast horizon. Treat the "
            "result as highly sensitive."
        )
    if equity_value <= 0:
        notes.append(
            "Net debt exceeds the discounted value of future cash flows, so the model "
            "returns no positive equity value."
        )

    upside = None
    if current_price and per_share > 0:
        upside = (per_share - current_price) / current_price

    return DcfResult(
        available=True,
        fair_value_per_share=per_share if per_share > 0 else None,
        equity_value=equity_value, enterprise_value=enterprise_value,
        terminal_value=terminal_value, terminal_share_of_value=terminal_share,
        upside=upside, projections=projections,
        assumptions=assumptions.to_dict(),
        notes=notes or [ASSUMPTION_NOTE],
    )


def sensitivity_grid(
    assumptions: DcfAssumptions, *,
    rate_steps: Sequence[float] = (-0.02, -0.01, 0.0, 0.01, 0.02),
    growth_steps: Sequence[float] = (-0.02, -0.01, 0.0, 0.01, 0.02),
) -> dict[str, Any]:
    """Fair value across discount-rate and terminal-growth variations.

    A single DCF number invites false confidence; the grid shows how wide the
    plausible range really is.
    """
    base_rate = assumptions.effective_discount_rate()
    rows: list[dict[str, Any]] = []
    for r_delta in rate_steps:
        rate = base_rate + r_delta
        cells: list[float | None] = []
        for g_delta in growth_steps:
            trial = DcfAssumptions(**{**assumptions.__dict__})
            trial.discount_rate = rate
            trial.terminal_growth = assumptions.terminal_growth + g_delta
            result = run_dcf(trial)
            cells.append(result.fair_value_per_share)
        rows.append({"discount_rate": rate, "values": cells})
    return {
        "rows": rows,
        "growth_rates": [assumptions.terminal_growth + g for g in growth_steps],
        "note": (
            "Each cell is a complete re-run of the model. Blank cells are "
            "combinations where terminal growth is not below the discount rate, "
            "which the model refuses to value."
        ),
    }


# ---------------------------------------------------------------------------
# Multiples
# ---------------------------------------------------------------------------
@dataclass
class MultipleValuation:
    method: str
    label: str
    fair_value: float | None
    basis: str
    inputs: dict[str, Any] = field(default_factory=dict)
    available: bool = True
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def multiples_valuation(
    *, eps: float | None = None, book_value_per_share: float | None = None,
    sales_per_share: float | None = None, fcf_per_share: float | None = None,
    peer_pe: float | None = None, peer_pb: float | None = None,
    peer_ps: float | None = None, own_pe_median: float | None = None,
) -> list[MultipleValuation]:
    """Fair value implied by peer and own-history multiples.

    Each row states which multiple and which per-share figure produced it, so a
    reader can reject any one of them on its own terms.
    """
    out: list[MultipleValuation] = []

    def add(method: str, label: str, per_share: float | None, multiple: float | None,
            basis: str, keys: dict[str, Any]) -> None:
        if per_share is None or multiple is None:
            out.append(MultipleValuation(
                method=method, label=label, fair_value=None, basis=basis, inputs=keys,
                available=False,
                unavailable_reason="N/A — data unavailable for this multiple.",
            ))
            return
        if per_share <= 0 or multiple <= 0:
            out.append(MultipleValuation(
                method=method, label=label, fair_value=None, basis=basis, inputs=keys,
                available=False,
                unavailable_reason=(
                    "Not meaningful: the multiple or the per-share figure is not positive."
                ),
            ))
            return
        out.append(MultipleValuation(
            method=method, label=label, fair_value=per_share * multiple,
            basis=basis, inputs=keys,
        ))

    add("peer_pe", "Sector P/E", eps, peer_pe,
        "EPS × the sector median price/earnings multiple",
        {"eps": eps, "sector_pe": peer_pe})
    add("own_pe", "Own historical P/E", eps, own_pe_median,
        "EPS × this company's own median P/E",
        {"eps": eps, "own_median_pe": own_pe_median})
    add("peer_pb", "Sector P/B", book_value_per_share, peer_pb,
        "Book value per share × the sector median price/book multiple",
        {"book_value_per_share": book_value_per_share, "sector_pb": peer_pb})
    add("peer_ps", "Sector P/S", sales_per_share, peer_ps,
        "Sales per share × the sector median price/sales multiple",
        {"sales_per_share": sales_per_share, "sector_ps": peer_ps})
    return out


# ---------------------------------------------------------------------------
# Blended view
# ---------------------------------------------------------------------------
#: When the highest method exceeds the lowest by more than this, the methods
#: disagree too much for their average to mean anything, and no single figure
#: is published.
MAX_METHOD_DISPERSION = 2.5


@dataclass
class ValuationSummary:
    current_price: float | None
    fair_value: float | None
    upside: float | None
    method_count: int
    low: float | None
    high: float | None
    methods: list[dict[str, Any]]
    dcf: dict[str, Any]
    note: str
    currency: str = "EGP"
    dispersion: float | None = None
    withheld: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def blended_valuation(
    *, current_price: float | None, dcf: DcfResult,
    multiples: Sequence[MultipleValuation], dcf_weight: float = 0.4,
    currency: str = "EGP",
) -> ValuationSummary:
    """Combine the methods that produced a number, ignoring those that did not.

    The count of contributing methods is reported: a "fair value" resting on one
    multiple deserves less confidence than one where four methods agree, and the
    page says which it is.
    """
    rows: list[dict[str, Any]] = []
    values: list[tuple[float, float]] = []

    if dcf.available and dcf.fair_value_per_share:
        rows.append({"method": "dcf", "label": "Discounted cash flow",
                     "fair_value": dcf.fair_value_per_share, "weight": dcf_weight,
                     "available": True})
        values.append((dcf.fair_value_per_share, dcf_weight))
    else:
        rows.append({"method": "dcf", "label": "Discounted cash flow",
                     "fair_value": None, "weight": 0.0, "available": False,
                     "reason": dcf.unavailable_reason})

    usable = [m for m in multiples if m.available and m.fair_value]
    each = (1.0 - dcf_weight) / len(usable) if usable else 0.0
    for m in multiples:
        if m.available and m.fair_value:
            rows.append({"method": m.method, "label": m.label,
                         "fair_value": m.fair_value, "weight": each, "available": True})
            values.append((m.fair_value, each))
        else:
            rows.append({"method": m.method, "label": m.label, "fair_value": None,
                         "weight": 0.0, "available": False,
                         "reason": m.unavailable_reason})

    if not values:
        return ValuationSummary(
            current_price=current_price, fair_value=None, upside=None, method_count=0,
            low=None, high=None, methods=rows, dcf=dcf.to_dict(), currency=currency,
            note=("N/A — data unavailable. No valuation method could be completed with "
                  "the data stored for this company."),
        )

    # Redistribute weight so the methods that worked sum to 1.
    total_weight = sum(w for _, w in values)
    fair = sum(v * w for v, w in values) / total_weight if total_weight else None
    raw = [v for v, _ in values]
    low, high = min(raw), max(raw)
    dispersion = (high / low) if low > 0 else None

    # When the methods disagree by more than the threshold, averaging them
    # manufactures a precision that does not exist. The range is reported
    # instead, and no headline fair value is published.
    if dispersion is not None and dispersion > MAX_METHOD_DISPERSION and len(values) > 1:
        return ValuationSummary(
            current_price=current_price, fair_value=None, upside=None,
            method_count=len(values), low=low, high=high, methods=rows,
            dcf=dcf.to_dict(), currency=currency, dispersion=dispersion, withheld=True,
            note=(
                f"No single fair value is published. The methods that completed disagree by "
                f"{dispersion:.1f}x — from {low:,.2f} to {high:,.2f} — and averaging them "
                "would invent a precision the data does not support. Look at the individual "
                "methods below and judge which assumptions you accept. " + ASSUMPTION_NOTE
            ),
        )

    upside = ((fair - current_price) / current_price) if (fair and current_price) else None

    note = ASSUMPTION_NOTE
    if len(values) == 1:
        note = (
            "Only one valuation method could be completed, so this figure rests entirely "
            "on that method's assumptions. " + ASSUMPTION_NOTE
        )
    return ValuationSummary(
        current_price=current_price, fair_value=fair, upside=upside,
        method_count=len(values), low=low, high=high, methods=rows,
        dcf=dcf.to_dict(), note=note, currency=currency, dispersion=dispersion,
    )
