"""Stock screener.

A screen filters on data that exists. A company whose P/E is unknown is not
silently treated as expensive or cheap — it is excluded from a P/E filter and
reported as excluded for lack of data, so a user can tell the difference
between "no company passed" and "we could not tell".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from sqlalchemy.orm import Session

from backend.data.models import Company, ScoreHistory
from backend.data.saas_models import Quote


@dataclass
class FilterSpec:
    key: str
    label: str
    unit: str            # ratio | percent | currency | score | times
    source: str          # quote | fundamental | score
    help: str = ""


#: Every filter the screener offers, with where its value comes from.
FILTERS: list[FilterSpec] = [
    FilterSpec("price", "Price (EGP)", "currency", "quote"),
    FilterSpec("change_pct", "Daily change", "percent", "quote"),
    FilterSpec("volume", "Volume", "currency", "quote"),
    FilterSpec("turnover", "Traded value (EGP)", "currency", "quote"),
    FilterSpec("market_cap", "Market capitalisation (EGP)", "currency", "fundamental"),
    FilterSpec("pe", "P/E", "times", "fundamental"),
    FilterSpec("pb", "P/B", "times", "fundamental"),
    FilterSpec("ev_ebitda", "EV/EBITDA", "times", "fundamental"),
    FilterSpec("dividend_yield", "Dividend yield", "percent", "fundamental"),
    FilterSpec("roe", "Return on equity", "percent", "fundamental"),
    FilterSpec("net_margin", "Net margin", "percent", "fundamental"),
    FilterSpec("revenue_growth", "Revenue growth", "percent", "fundamental"),
    FilterSpec("debt_to_equity", "Debt to equity", "times", "fundamental"),
    FilterSpec("current_ratio", "Current ratio", "times", "fundamental"),
    FilterSpec("fcf_yield", "Free cash flow yield", "percent", "fundamental"),
    FilterSpec("alpha_score", "GMG score", "score", "score"),
    FilterSpec("fundamental_score", "Fundamental score", "score", "score"),
    FilterSpec("technical_score", "Technical score", "score", "score"),
    FilterSpec("rsi14", "RSI (14)", "ratio", "score"),
]

FILTER_BY_KEY = {f.key: f for f in FILTERS}


@dataclass
class ScreenResult:
    ticker: str
    name: str
    sector: str | None
    values: dict[str, float | None]
    is_demo: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ScreenRun:
    rows: list[ScreenResult]
    universe: int
    excluded_for_missing_data: dict[str, int] = field(default_factory=dict)
    filters_applied: list[dict[str, Any]] = field(default_factory=list)
    is_demo: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "universe": self.universe,
            "excluded_for_missing_data": self.excluded_for_missing_data,
            "filters_applied": self.filters_applied,
            "is_demo": self.is_demo,
            "note": self.note,
        }


def _row_values(
    company: Company, quote: Quote | None, score: ScoreHistory | None,
    metrics: dict[str, Any] | None,
) -> dict[str, float | None]:
    """Assemble every screenable value for one company. Missing stays None."""
    values: dict[str, float | None] = {key: None for key in FILTER_BY_KEY}
    if quote is not None:
        values["price"] = quote.price
        values["change_pct"] = quote.change_pct
        values["volume"] = quote.volume
        values["turnover"] = quote.turnover
        values["market_cap"] = quote.market_cap
    if score is not None:
        values["alpha_score"] = score.alpha_score
        values["fundamental_score"] = score.fundamental_score
        values["technical_score"] = score.technical_score
    if metrics:
        for key in ("pe", "pb", "ev_ebitda", "dividend_yield", "roe", "net_margin",
                    "revenue_growth", "debt_to_equity", "current_ratio", "fcf_yield",
                    "market_cap"):
            value = metrics.get(key)
            if value is not None and values.get(key) is None:
                values[key] = value
    return values


def run_screen(
    session: Session, *, companies: Sequence[Company], quotes: dict[str, Quote],
    scores: dict[str, ScoreHistory], metrics: dict[str, dict[str, Any]] | None = None,
    criteria: Sequence[dict[str, Any]] = (), sectors: Sequence[str] = (),
    indices: Sequence[str] = (), sort_key: str = "alpha_score",
    descending: bool = True, limit: int = 100,
) -> ScreenRun:
    """Apply the criteria and return the companies that pass.

    Each criterion is ``{"key": ..., "op": "gte"|"lte", "value": float}``.
    """
    metrics = metrics or {}
    excluded: dict[str, int] = {}
    rows: list[ScreenResult] = []
    demo = False

    index_attr = {"EGX30": "in_egx30", "EGX70": "in_egx70", "EGX100": "in_egx100"}

    for company in companies:
        if sectors and (company.sector or "Unclassified") not in sectors:
            continue
        if indices and not any(
            getattr(company, index_attr[i], False) for i in indices if i in index_attr
        ):
            continue

        quote = quotes.get(company.ticker)
        values = _row_values(company, quote, scores.get(company.ticker),
                             metrics.get(company.ticker))
        if quote is not None and quote.is_demo:
            demo = True

        passed = True
        for criterion in criteria:
            key = criterion.get("key")
            spec = FILTER_BY_KEY.get(key)
            if spec is None:
                continue
            value = values.get(key)
            if value is None:
                # Unknown is not a pass and not a fail-by-default: it is counted
                # so the user can see how much of the market could not be tested.
                excluded[key] = excluded.get(key, 0) + 1
                passed = False
                break
            bound = criterion.get("value")
            if bound is None:
                continue
            if criterion.get("op") == "lte" and value > bound:
                passed = False
                break
            if criterion.get("op") == "gte" and value < bound:
                passed = False
                break
        if passed:
            rows.append(ScreenResult(
                ticker=company.ticker, name=company.name, sector=company.sector,
                values=values, is_demo=bool(quote.is_demo) if quote else False,
            ))

    def sort_value(row: ScreenResult) -> float:
        value = row.values.get(sort_key)
        if value is None:
            return float("-inf") if descending else float("inf")
        return float(value)

    rows.sort(key=sort_value, reverse=descending)

    note = ""
    if excluded:
        total = sum(excluded.values())
        note = (
            f"{total} company-criterion checks could not be evaluated because the value "
            "was not available. Those companies were excluded rather than assumed to pass."
        )
    return ScreenRun(
        rows=rows[:limit], universe=len(companies), excluded_for_missing_data=excluded,
        filters_applied=list(criteria), is_demo=demo, note=note,
    )
