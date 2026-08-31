"""Fundamental analysis engine.

Computes growth, profitability, balance-sheet, cash-flow and valuation metrics
from stored financial statements, then scores them 0-100.

Every metric is a CALCULATION over reported figures. Where an input is absent
the metric is ``UNAVAILABLE`` — never estimated, never defaulted. Valuation is
compared against the company's own history, its sector peers, and the market,
because a P/E of 12 means different things for a bank and a developer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from backend.analytics.scoring import (
    ScoreComponent,
    ScoreResult,
    build_score,
    normalize_bands,
    normalize_linear,
    percentile_rank,
)
from backend.core.config import load_yaml_config
from backend.core.data_quality import (
    UNAVAILABLE,
    Confidence,
    is_available,
    safe_div,
    safe_growth,
)


@dataclass
class FinancialPeriod:
    """A single reported period, normalised for the engine's use."""

    ticker: str
    period: str
    period_type: str
    period_end: date
    available_from: date | None = None
    revenue: float | None = None
    gross_profit: float | None = None
    ebitda: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    eps: float | None = None
    cash: float | None = None
    total_debt: float | None = None
    total_assets: float | None = None
    total_equity: float | None = None
    operating_cash_flow: float | None = None
    capex: float | None = None
    free_cash_flow: float | None = None
    interest_expense: float | None = None
    current_assets: float | None = None
    current_liabilities: float | None = None
    dividends_paid: float | None = None
    source: str = "UNKNOWN"
    confidence: str = "HIGH"

    @classmethod
    def from_model(cls, row: Any) -> "FinancialPeriod":
        return cls(
            ticker=row.ticker, period=row.period, period_type=row.period_type,
            period_end=row.period_end, available_from=row.available_from,
            revenue=row.revenue, gross_profit=row.gross_profit, ebitda=row.ebitda,
            operating_income=row.operating_income, net_income=row.net_income,
            eps=row.eps, cash=row.cash, total_debt=row.total_debt,
            total_assets=row.total_assets, total_equity=row.total_equity,
            operating_cash_flow=row.operating_cash_flow, capex=row.capex,
            free_cash_flow=row.free_cash_flow, interest_expense=row.interest_expense,
            current_assets=row.current_assets, current_liabilities=row.current_liabilities,
            dividends_paid=row.dividends_paid,
            source=row.source, confidence=row.confidence,
        )


@dataclass
class Metric:
    """One computed fundamental metric, with the inputs that produced it."""

    name: str
    value: Any
    unit: str = "ratio"        # ratio | percent | currency | times | years
    inputs: dict[str, Any] = field(default_factory=dict)
    period: str | None = None
    note: str | None = None

    @property
    def available(self) -> bool:
        return is_available(self.value)

    def formatted(self) -> str:
        if not self.available:
            return "—"
        if self.unit == "percent":
            return f"{self.value:.1%}"
        if self.unit == "times":
            return f"{self.value:.2f}x"
        if self.unit == "currency":
            return f"{self.value:,.0f}"
        return f"{self.value:.2f}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value if self.available else None,
            "available": self.available,
            "formatted": self.formatted(),
            "unit": self.unit,
            "inputs": self.inputs,
            "period": self.period,
            "note": self.note,
        }


@dataclass
class FundamentalSnapshot:
    ticker: str
    latest_period: str | None = None
    period_end: date | None = None
    available_from: date | None = None
    metrics: dict[str, Metric] = field(default_factory=dict)
    score: ScoreResult | None = None
    peer_context: dict[str, Any] = field(default_factory=dict)
    history_context: dict[str, Any] = field(default_factory=dict)
    insufficient_data: bool = False
    note: str | None = None
    source: str = "UNKNOWN"
    confidence: str = "HIGH"

    def get(self, name: str) -> Any:
        metric = self.metrics.get(name)
        return metric.value if metric and metric.available else UNAVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "latest_period": self.latest_period,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "available_from": self.available_from.isoformat() if self.available_from else None,
            "insufficient_data": self.insufficient_data,
            "note": self.note,
            "source": self.source,
            "confidence": self.confidence,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "score": self.score.to_dict() if self.score else None,
            "peer_context": self.peer_context,
            "history_context": self.history_context,
        }



# ---------------------------------------------------------------------------
# Unit-scale safety
# ---------------------------------------------------------------------------
#: Plausible ranges for derived multiples on any real listed equity. A value
#: outside these bounds is almost always a units mismatch between the price
#: (absolute currency) and the statements (often reported in thousands or
#: millions), not a real valuation. Publishing "P/B = 1,777,777x" would be a
#: false claim, so such metrics are withheld and the mismatch is reported.
PLAUSIBLE_MULTIPLE_BOUNDS: dict[str, tuple[float, float]] = {
    "pe": (0.1, 1000.0),
    "pb": (0.01, 200.0),
    "ps": (0.001, 500.0),
    "ev_ebitda": (0.1, 500.0),
    "ev_sales": (0.001, 500.0),
}


def detect_scale_mismatch(
    market_cap: Any, revenue: Any, total_equity: Any
) -> float | None:
    """Infer the statement scale factor when it disagrees with the price scale.

    Returns the power-of-1000 factor the statements appear to be expressed in
    (1e3 for thousands, 1e6 for millions), or ``None`` when the scales already
    agree or cannot be determined.
    """
    if not is_available(market_cap) or market_cap <= 0:
        return None
    reference = revenue if is_available(revenue) and revenue > 0 else total_equity
    if not is_available(reference) or reference <= 0:
        return None
    ratio = market_cap / reference
    # A genuine P/S above ~500x does not occur on a listed operating company.
    for factor in (1e3, 1e6, 1e9):
        if 0.02 <= ratio / factor <= 500.0:
            return factor
    return None


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------
def compute_metrics(
    periods: Sequence[FinancialPeriod],
    *,
    price: float | None = None,
    shares_outstanding: float | None = None,
    statement_scale: float = 1.0,
) -> dict[str, Metric]:
    """Compute all fundamental metrics from *periods* (newest first).

    *statement_scale* is the multiplier converting reported statement figures to
    the same currency units as *price* — use 1e3 for statements in thousands,
    1e6 for millions. When left at 1.0 the engine detects an obvious mismatch
    itself and withholds the affected multiples rather than publishing nonsense.
    """
    metrics: dict[str, Metric] = {}
    if not periods:
        return metrics

    latest = periods[0]
    prior = periods[1] if len(periods) > 1 else None
    tag = latest.period

    def put(name: str, value: Any, unit: str = "ratio", inputs: dict | None = None,
            note: str | None = None) -> None:
        metrics[name] = Metric(name, value, unit, inputs or {}, tag, note)

    # --- Growth (year over year) --------------------------------------------
    if prior:
        put("revenue_growth", safe_growth(latest.revenue, prior.revenue), "percent",
            {"current": latest.revenue, "prior": prior.revenue, "prior_period": prior.period})
        put("ebitda_growth", safe_growth(latest.ebitda, prior.ebitda), "percent",
            {"current": latest.ebitda, "prior": prior.ebitda})
        put("net_income_growth", safe_growth(latest.net_income, prior.net_income), "percent",
            {"current": latest.net_income, "prior": prior.net_income},
            note="Undefined when the prior period was a loss." )
        put("eps_growth", safe_growth(latest.eps, prior.eps), "percent",
            {"current": latest.eps, "prior": prior.eps})
        put("fcf_growth", safe_growth(latest.free_cash_flow, prior.free_cash_flow), "percent",
            {"current": latest.free_cash_flow, "prior": prior.free_cash_flow})

    # Multi-year revenue CAGR gives a cleaner growth read than a single year.
    if len(periods) >= 3:
        oldest = periods[min(len(periods) - 1, 4)]
        years = (latest.period_end - oldest.period_end).days / 365.25
        if years >= 1 and is_available(latest.revenue) and is_available(oldest.revenue) and oldest.revenue > 0:
            cagr = (latest.revenue / oldest.revenue) ** (1 / years) - 1
            put("revenue_cagr", cagr, "percent",
                {"from": oldest.period, "to": latest.period, "years": round(years, 1)})

    # --- Profitability -------------------------------------------------------
    put("gross_margin", safe_div(latest.gross_profit, latest.revenue), "percent",
        {"gross_profit": latest.gross_profit, "revenue": latest.revenue})
    put("ebitda_margin", safe_div(latest.ebitda, latest.revenue), "percent",
        {"ebitda": latest.ebitda, "revenue": latest.revenue})
    put("operating_margin", safe_div(latest.operating_income, latest.revenue), "percent",
        {"operating_income": latest.operating_income, "revenue": latest.revenue})
    put("net_margin", safe_div(latest.net_income, latest.revenue), "percent",
        {"net_income": latest.net_income, "revenue": latest.revenue})

    # ROE/ROA on average equity/assets when a prior period exists — a point-in-time
    # denominator overstates returns for a company that grew its balance sheet.
    avg_equity = _average(latest.total_equity, prior.total_equity if prior else None)
    avg_assets = _average(latest.total_assets, prior.total_assets if prior else None)
    put("roe", safe_div(latest.net_income, avg_equity, allow_negative_denom=False), "percent",
        {"net_income": latest.net_income, "average_equity": avg_equity})
    put("roa", safe_div(latest.net_income, avg_assets, allow_negative_denom=False), "percent",
        {"net_income": latest.net_income, "average_assets": avg_assets})

    # ROIC = NOPAT / invested capital. Egypt's statutory corporate rate is 22.5%.
    invested_capital = _sum_available(latest.total_equity, latest.total_debt, -(latest.cash or 0)
                                      if is_available(latest.cash) else None)
    nopat = latest.operating_income * (1 - 0.225) if is_available(latest.operating_income) else UNAVAILABLE
    put("roic", safe_div(nopat, invested_capital, allow_negative_denom=False), "percent",
        {"operating_income": latest.operating_income, "tax_rate": 0.225,
         "invested_capital": invested_capital},
        note="NOPAT uses Egypt's 22.5% statutory corporate rate, not an effective rate.")

    # --- Balance sheet -------------------------------------------------------
    put("debt_to_equity", safe_div(latest.total_debt, latest.total_equity,
                                   allow_negative_denom=False), "times",
        {"total_debt": latest.total_debt, "total_equity": latest.total_equity})
    net_debt = (
        latest.total_debt - latest.cash
        if is_available(latest.total_debt) and is_available(latest.cash)
        else UNAVAILABLE
    )
    put("net_debt", net_debt, "currency",
        {"total_debt": latest.total_debt, "cash": latest.cash})
    put("net_debt_to_ebitda", safe_div(net_debt, latest.ebitda, allow_negative_denom=False), "times",
        {"net_debt": net_debt, "ebitda": latest.ebitda})
    put("current_ratio", safe_div(latest.current_assets, latest.current_liabilities), "times",
        {"current_assets": latest.current_assets, "current_liabilities": latest.current_liabilities})
    put("interest_coverage",
        safe_div(latest.operating_income, abs(latest.interest_expense)
                 if is_available(latest.interest_expense) else UNAVAILABLE), "times",
        {"operating_income": latest.operating_income, "interest_expense": latest.interest_expense})
    put("equity_ratio", safe_div(latest.total_equity, latest.total_assets), "percent",
        {"total_equity": latest.total_equity, "total_assets": latest.total_assets})

    # --- Cash flow -----------------------------------------------------------
    put("operating_cash_flow", latest.operating_cash_flow if is_available(latest.operating_cash_flow)
        else UNAVAILABLE, "currency", {"reported": latest.operating_cash_flow})
    fcf = latest.free_cash_flow
    if not is_available(fcf) and is_available(latest.operating_cash_flow) and is_available(latest.capex):
        fcf = latest.operating_cash_flow - abs(latest.capex)
    put("free_cash_flow", fcf if is_available(fcf) else UNAVAILABLE, "currency",
        {"operating_cash_flow": latest.operating_cash_flow, "capex": latest.capex})
    put("fcf_margin", safe_div(fcf, latest.revenue), "percent",
        {"free_cash_flow": fcf, "revenue": latest.revenue})
    # Cash conversion: how much of reported profit arrives as cash.
    put("cash_conversion", safe_div(latest.operating_cash_flow, latest.net_income,
                                    allow_negative_denom=False), "times",
        {"operating_cash_flow": latest.operating_cash_flow, "net_income": latest.net_income},
        note="Operating cash flow divided by net income. Below 1.0 warrants scrutiny.")
    put("capex_intensity", safe_div(abs(latest.capex) if is_available(latest.capex) else UNAVAILABLE,
                                    latest.revenue), "percent",
        {"capex": latest.capex, "revenue": latest.revenue})

    # --- Valuation (requires a price and a share count) ----------------------
    market_cap = price * shares_outstanding if (price and shares_outstanding) else UNAVAILABLE

    # Reconcile statement units with price units before computing any multiple.
    scale = statement_scale
    scale_note: str | None = None
    if scale == 1.0:
        detected = detect_scale_mismatch(market_cap, latest.revenue, latest.total_equity)
        if detected:
            scale = detected
            scale_note = (
                f"Statement figures appear to be reported in units of {detected:,.0f}; "
                "multiples were rescaled accordingly. Set statement_scale explicitly "
                "to remove this inference."
            )

    def scaled(x: Any) -> Any:
        return x * scale if is_available(x) else UNAVAILABLE

    put("market_cap", market_cap, "currency",
        {"price": price, "shares_outstanding": shares_outstanding})
    if scale_note:
        put("statement_scale", scale, "ratio", {"detected": True}, note=scale_note)
    put("pe", safe_div(price, latest.eps, allow_negative_denom=False), "times",
        {"price": price, "eps": latest.eps},
        note="Not computed for loss-making periods — a negative P/E is not a multiple.")
    put("pb", safe_div(market_cap, scaled(latest.total_equity), allow_negative_denom=False), "times",
        {"market_cap": market_cap, "total_equity": latest.total_equity, "scale": scale})
    put("ps", safe_div(market_cap, scaled(latest.revenue), allow_negative_denom=False), "times",
        {"market_cap": market_cap, "revenue": latest.revenue, "scale": scale})
    enterprise_value = (
        market_cap + scaled(net_debt)
        if is_available(market_cap) and is_available(net_debt) else UNAVAILABLE
    )
    put("enterprise_value", enterprise_value, "currency",
        {"market_cap": market_cap, "net_debt": net_debt, "scale": scale})
    put("ev_ebitda", safe_div(enterprise_value, scaled(latest.ebitda), allow_negative_denom=False), "times",
        {"enterprise_value": enterprise_value, "ebitda": latest.ebitda, "scale": scale})
    put("ev_sales", safe_div(enterprise_value, scaled(latest.revenue), allow_negative_denom=False), "times",
        {"enterprise_value": enterprise_value, "revenue": latest.revenue, "scale": scale})
    put("fcf_yield", safe_div(scaled(fcf), market_cap), "percent",
        {"free_cash_flow": fcf, "market_cap": market_cap, "scale": scale})
    put("earnings_yield", safe_div(latest.eps, price), "percent",
        {"eps": latest.eps, "price": price})
    if is_available(latest.dividends_paid) and is_available(market_cap):
        put("dividend_yield", safe_div(scaled(abs(latest.dividends_paid)), market_cap), "percent",
            {"dividends_paid": latest.dividends_paid, "market_cap": market_cap, "scale": scale})
    else:
        put("dividend_yield", UNAVAILABLE, "percent",
            {"dividends_paid": latest.dividends_paid, "market_cap": market_cap})
    # Payout ratio: dividends paid against net income. Above 1.0 means the
    # company distributed more than it earned in the period.
    if is_available(latest.dividends_paid) and is_available(latest.net_income):
        put("payout_ratio",
            safe_div(abs(latest.dividends_paid), latest.net_income, allow_negative_denom=False),
            "percent",
            {"dividends_paid": latest.dividends_paid, "net_income": latest.net_income})
    else:
        put("payout_ratio", UNAVAILABLE, "percent",
            {"dividends_paid": latest.dividends_paid, "net_income": latest.net_income})

    # Final guard: withhold any multiple that is not physically plausible. This
    # is the backstop for a units problem the scale detector could not resolve.
    for name, (low, high) in PLAUSIBLE_MULTIPLE_BOUNDS.items():
        metric = metrics.get(name)
        if metric and metric.available and not (low <= metric.value <= high):
            metrics[name] = Metric(
                name, UNAVAILABLE, metric.unit, metric.inputs, tag,
                note=(
                    f"Withheld: computed value {metric.value:,.1f} falls outside the "
                    f"plausible range {low}-{high} for this multiple, which indicates "
                    "a units mismatch between price and reported statements rather "
                    "than a real valuation. Set statement_scale explicitly."
                ),
            )

    return metrics


def _average(a: float | None, b: float | None) -> Any:
    if is_available(a) and is_available(b):
        return (a + b) / 2.0
    if is_available(a):
        return a
    return UNAVAILABLE


def _sum_available(*values: Any) -> Any:
    present = [v for v in values if is_available(v)]
    return sum(present) if present else UNAVAILABLE


# ---------------------------------------------------------------------------
# Fundamental score
# ---------------------------------------------------------------------------
def score_fundamentals(
    metrics: dict[str, Metric],
    *,
    peer_metrics: dict[str, list[float]] | None = None,
    history: dict[str, list[float]] | None = None,
    catalyst_score: float | None = None,
    weights: dict[str, float] | None = None,
) -> ScoreResult:
    """Build the 0-100 fundamental score from computed metrics.

    *peer_metrics* maps metric name → sector peer values, enabling relative
    valuation. *history* maps metric name → the company's own past values.
    """
    cfg = weights or load_yaml_config("weights").get("fundamental") or {}
    peer_metrics = peer_metrics or {}
    history = history or {}

    def value(name: str) -> Any:
        metric = metrics.get(name)
        return metric.value if metric and metric.available else None

    components: list[ScoreComponent] = []

    # --- Valuation (25%) -----------------------------------------------------
    # Blends absolute level, sector-relative position, and the company's own history.
    valuation_parts: list[tuple[float, float]] = []  # (score, weight)
    valuation_inputs: dict[str, Any] = {}

    pe = value("pe")
    if pe is not None:
        # 5x → 100, 30x → 0. EGX has historically traded at a discount to EM peers.
        absolute = normalize_linear(pe, 30.0, 5.0)
        valuation_parts.append((absolute, 0.4))
        valuation_inputs["pe"] = round(pe, 2)
        peers = peer_metrics.get("pe")
        if peers:
            # Cheaper than peers = better, so invert the percentile.
            rank = percentile_rank(pe, peers)
            if rank is not None:
                valuation_parts.append((100.0 - rank, 0.35))
                valuation_inputs["pe_vs_sector_percentile"] = round(100.0 - rank, 1)
                valuation_inputs["sector_median_pe"] = round(_median(peers), 2)
        own = history.get("pe")
        if own and len(own) >= 3:
            rank = percentile_rank(pe, own)
            if rank is not None:
                valuation_parts.append((100.0 - rank, 0.25))
                valuation_inputs["pe_vs_own_history_percentile"] = round(100.0 - rank, 1)

    ev_ebitda = value("ev_ebitda")
    if ev_ebitda is not None:
        valuation_parts.append((normalize_linear(ev_ebitda, 18.0, 3.0), 0.3))
        valuation_inputs["ev_ebitda"] = round(ev_ebitda, 2)

    fcf_yield = value("fcf_yield")
    if fcf_yield is not None:
        valuation_parts.append((normalize_linear(fcf_yield, 0.0, 0.18), 0.3))
        valuation_inputs["fcf_yield"] = f"{fcf_yield:.2%}"

    pb = value("pb")
    if pb is not None:
        valuation_parts.append((normalize_linear(pb, 4.0, 0.5), 0.2))
        valuation_inputs["pb"] = round(pb, 2)

    components.append(ScoreComponent(
        "valuation", cfg.get("valuation", 25), _blend(valuation_parts),
        inputs=valuation_inputs,
        explanation=(
            "Absolute multiples blended with sector-relative and own-history "
            "percentile position. Cheaper scores higher."
        ),
    ))

    # --- Business quality (20%) ---------------------------------------------
    quality_parts: list[tuple[float, float]] = []
    quality_inputs: dict[str, Any] = {}
    roe, roic, gross_margin = value("roe"), value("roic"), value("gross_margin")
    if roe is not None:
        quality_parts.append((normalize_linear(roe, 0.0, 0.30), 0.35))
        quality_inputs["roe"] = f"{roe:.1%}"
    if roic is not None:
        quality_parts.append((normalize_linear(roic, 0.0, 0.25), 0.35))
        quality_inputs["roic"] = f"{roic:.1%}"
    if gross_margin is not None:
        quality_parts.append((normalize_linear(gross_margin, 0.05, 0.55), 0.15))
        quality_inputs["gross_margin"] = f"{gross_margin:.1%}"
    cash_conversion = value("cash_conversion")
    if cash_conversion is not None:
        # Earnings that convert to cash are higher quality earnings.
        quality_parts.append((normalize_linear(cash_conversion, 0.3, 1.4), 0.15))
        quality_inputs["cash_conversion"] = f"{cash_conversion:.2f}x"
    components.append(ScoreComponent(
        "quality", cfg.get("quality", 20), _blend(quality_parts), inputs=quality_inputs,
        explanation="Returns on equity and invested capital, margin level, and earnings-to-cash conversion.",
    ))

    # --- Growth (15%) --------------------------------------------------------
    growth_parts: list[tuple[float, float]] = []
    growth_inputs: dict[str, Any] = {}
    for key, weight, floor, ceiling in (
        ("revenue_growth", 0.3, -0.10, 0.35),
        ("revenue_cagr", 0.25, -0.05, 0.30),
        ("ebitda_growth", 0.2, -0.15, 0.40),
        ("eps_growth", 0.25, -0.20, 0.45),
    ):
        v = value(key)
        if v is not None:
            growth_parts.append((normalize_linear(v, floor, ceiling), weight))
            growth_inputs[key] = f"{v:+.1%}"
    components.append(ScoreComponent(
        "growth", cfg.get("growth", 15), _blend(growth_parts), inputs=growth_inputs,
        explanation="Revenue, EBITDA and EPS growth plus multi-year revenue CAGR.",
    ))

    # --- Profitability (15%) -------------------------------------------------
    profit_parts: list[tuple[float, float]] = []
    profit_inputs: dict[str, Any] = {}
    for key, weight, floor, ceiling in (
        ("net_margin", 0.35, 0.0, 0.25),
        ("operating_margin", 0.30, 0.0, 0.30),
        ("ebitda_margin", 0.20, 0.02, 0.35),
        ("gross_margin", 0.15, 0.05, 0.55),
    ):
        v = value(key)
        if v is not None:
            profit_parts.append((normalize_linear(v, floor, ceiling), weight))
            profit_inputs[key] = f"{v:.1%}"
    components.append(ScoreComponent(
        "profitability", cfg.get("profitability", 15), _blend(profit_parts),
        inputs=profit_inputs, explanation="Margin structure from gross through to net.",
    ))

    # --- Balance sheet (10%) -------------------------------------------------
    bs_parts: list[tuple[float, float]] = []
    bs_inputs: dict[str, Any] = {}
    de = value("debt_to_equity")
    if de is not None:
        bs_parts.append((normalize_bands(de, [(0.2, 100), (0.5, 88), (1.0, 72),
                                              (1.5, 52), (2.5, 28), (4.0, 10)]) or 5.0, 0.35))
        bs_inputs["debt_to_equity"] = f"{de:.2f}x"
    nde = value("net_debt_to_ebitda")
    if nde is not None:
        bs_parts.append((normalize_bands(nde, [(0.0, 100), (1.0, 90), (2.0, 75),
                                               (3.0, 55), (4.0, 32), (5.0, 15)]) or 5.0, 0.35))
        bs_inputs["net_debt_to_ebitda"] = f"{nde:.2f}x"
    cr = value("current_ratio")
    if cr is not None:
        bs_parts.append((normalize_linear(cr, 0.6, 2.2), 0.15))
        bs_inputs["current_ratio"] = f"{cr:.2f}x"
    ic = value("interest_coverage")
    if ic is not None:
        bs_parts.append((normalize_linear(ic, 1.0, 12.0), 0.15))
        bs_inputs["interest_coverage"] = f"{ic:.1f}x"
    components.append(ScoreComponent(
        "balance_sheet", cfg.get("balance_sheet", 10), _blend(bs_parts), inputs=bs_inputs,
        explanation="Leverage, net debt to EBITDA, liquidity and interest cover.",
    ))

    # --- Cash flow (10%) -----------------------------------------------------
    cf_parts: list[tuple[float, float]] = []
    cf_inputs: dict[str, Any] = {}
    fcf_margin = value("fcf_margin")
    if fcf_margin is not None:
        cf_parts.append((normalize_linear(fcf_margin, -0.05, 0.20), 0.4))
        cf_inputs["fcf_margin"] = f"{fcf_margin:.1%}"
    if cash_conversion is not None:
        cf_parts.append((normalize_linear(cash_conversion, 0.3, 1.4), 0.35))
        cf_inputs["cash_conversion"] = f"{cash_conversion:.2f}x"
    fcf_growth = value("fcf_growth")
    if fcf_growth is not None:
        cf_parts.append((normalize_linear(fcf_growth, -0.25, 0.35), 0.25))
        cf_inputs["fcf_growth"] = f"{fcf_growth:+.1%}"
    components.append(ScoreComponent(
        "cash_flow", cfg.get("cash_flow", 10), _blend(cf_parts), inputs=cf_inputs,
        explanation="Free cash flow margin, conversion of earnings to cash, and FCF growth.",
    ))

    # --- Dividend (5%) -------------------------------------------------------
    # A distinct component because Egyptian retail investors weigh income
    # heavily, and a high yield funded by borrowing is not the same as one
    # funded by cash flow: payout and cover are scored alongside the yield.
    div_parts: list[tuple[float, float]] = []
    div_inputs: dict[str, Any] = {}
    dividend_yield = value("dividend_yield")
    if dividend_yield is not None:
        # 0% -> 0, 10% -> 100. Yields far above that usually signal a falling
        # price or a payout that will not repeat, so the scale is capped.
        div_parts.append((normalize_linear(dividend_yield, 0.0, 0.10), 0.55))
        div_inputs["dividend_yield"] = f"{dividend_yield:.2%}"
        if fcf_margin is not None:
            # Income backed by free cash flow scores better than income that is not.
            div_parts.append((normalize_linear(fcf_margin, -0.05, 0.20), 0.2))
            div_inputs["fcf_margin"] = f"{fcf_margin:.1%}"
        payout = value("payout_ratio")
        if payout is not None:
            # Comfortable below ~60%; above 100% the company is paying out more
            # than it earns, which is scored down rather than rewarded.
            div_parts.append((normalize_linear(payout, 1.10, 0.30), 0.25))
            div_inputs["payout_ratio"] = f"{payout:.1%}"
    components.append(ScoreComponent(
        "dividend", cfg.get("dividend", 5), _blend(div_parts), inputs=div_inputs,
        explanation=(
            "Dividend yield, weighed against the payout ratio and free cash flow "
            "that has to fund it. Withheld when no dividend data is available."
        ),
    ))

    # --- Catalysts (5%) ------------------------------------------------------
    components.append(ScoreComponent(
        "catalysts", cfg.get("catalysts", 5), catalyst_score,
        inputs={"catalyst_score": catalyst_score},
        explanation="Derived from recent disclosures and corporate events (see the event engine).",
    ))

    return build_score("fundamental", components)


def _blend(parts: list[tuple[float, float]]) -> float | None:
    """Weighted mean of ``(score, weight)`` pairs; ``None`` when empty."""
    usable = [(s, w) for s, w in parts if s is not None]
    if not usable:
        return None
    total = sum(w for _, w in usable)
    return sum(s * w for s, w in usable) / total if total > 0 else None


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def analyze_fundamentals(
    ticker: str,
    periods: Sequence[FinancialPeriod],
    *,
    price: float | None = None,
    shares_outstanding: float | None = None,
    peer_metrics: dict[str, list[float]] | None = None,
    history: dict[str, list[float]] | None = None,
    catalyst_score: float | None = None,
    statement_scale: float = 1.0,
) -> FundamentalSnapshot:
    """Full fundamental analysis for one company."""
    snapshot = FundamentalSnapshot(ticker=ticker)
    if not periods:
        snapshot.insufficient_data = True
        snapshot.note = (
            "No financial statements are available for this company. Fundamental "
            "analysis cannot be performed. Supply data via data/manual/fundamentals/."
        )
        snapshot.confidence = Confidence.UNVERIFIED.value
        return snapshot

    latest = periods[0]
    snapshot.latest_period = latest.period
    snapshot.period_end = latest.period_end
    snapshot.available_from = latest.available_from
    snapshot.source = latest.source
    snapshot.confidence = latest.confidence
    snapshot.metrics = compute_metrics(
        periods, price=price, shares_outstanding=shares_outstanding,
        statement_scale=statement_scale,
    )
    snapshot.score = score_fundamentals(
        snapshot.metrics, peer_metrics=peer_metrics, history=history,
        catalyst_score=catalyst_score,
    )
    if len(periods) < 2:
        snapshot.note = (
            "Only one reporting period is available, so growth metrics could not "
            "be computed. Their weight was redistributed."
        )
    snapshot.peer_context = {
        k: {"median": round(_median(v), 3), "count": len(v)}
        for k, v in (peer_metrics or {}).items() if v
    }
    snapshot.history_context = {
        k: {"count": len(v), "median": round(_median(v), 3)}
        for k, v in (history or {}).items() if v
    }
    return snapshot
