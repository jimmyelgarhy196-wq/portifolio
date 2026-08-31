"""The GMG master score.

Combines the fundamental, technical and quantitative engines with catalyst,
quality, risk and sentiment components into a single 0-100 number.

The AI is never permitted to produce this number. It is computed here, in
Python, from stored data, and it is returned with a complete decomposition so
any value can be traced to the rows that produced it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence

from backend.analytics.fundamental import FundamentalSnapshot
from backend.analytics.quant import QuantSnapshot
from backend.analytics.scoring import (
    ScoreComponent,
    ScoreResult,
    build_score,
    normalize_linear,
)
from backend.analytics.technical import TechnicalSnapshot
from backend.core.config import load_yaml_config
from backend.core.data_quality import Confidence, StalenessFinding, assess_staleness

#: Disclosure types that constitute a genuine catalyst, with their base weight.
CATALYST_WEIGHTS: dict[str, float] = {
    "M&A": 95.0,
    "EARNINGS": 75.0,
    "BUYBACK": 85.0,
    "DIVIDEND": 70.0,
    "CONTRACT": 78.0,
    "CAPITAL_ACTION": 65.0,
    "RESTRUCTURING": 60.0,
    "MANAGEMENT_CHANGE": 55.0,
    "REGULATORY": 45.0,
    "GOVERNANCE": 45.0,
    "OTHER": 40.0,
}


@dataclass
class CatalystEvent:
    kind: str
    title: str
    event_date: date | None
    importance: int
    source: str
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "date": self.event_date.isoformat() if self.event_date else None,
            "importance": self.importance,
            "source": self.source,
            "url": self.url,
        }


@dataclass
class RiskInputs:
    """Risk factors specific to a single security (portfolio risk lives elsewhere)."""

    volatility_annual: float | None = None
    beta: float | None = None
    max_drawdown_1y: float | None = None
    average_turnover: float | None = None
    debt_to_equity: float | None = None
    net_debt_to_ebitda: float | None = None
    interest_coverage: float | None = None
    data_staleness_days: float | None = None
    earnings_within_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class AlphaScore:
    ticker: str
    as_of: date
    score: ScoreResult
    fundamental: ScoreResult | None = None
    technical: ScoreResult | None = None
    quant: ScoreResult | None = None
    catalyst: ScoreResult | None = None
    risk: ScoreResult | None = None
    sentiment_value: float | None = None
    quality_value: float | None = None
    catalysts: list[CatalystEvent] = field(default_factory=list)
    staleness: list[StalenessFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def value(self) -> float | None:
        return self.score.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of.isoformat(),
            "alpha_score": None if self.value is None else round(self.value, 1),
            "confidence": self.score.confidence.value,
            "coverage": round(self.score.coverage, 3),
            "score": self.score.to_dict(),
            "sub_scores": {
                "fundamental": self.fundamental.to_dict() if self.fundamental else None,
                "technical": self.technical.to_dict() if self.technical else None,
                "quantitative": self.quant.to_dict() if self.quant else None,
                "catalyst": self.catalyst.to_dict() if self.catalyst else None,
                "risk": self.risk.to_dict() if self.risk else None,
            },
            "sentiment": self.sentiment_value,
            "quality": self.quality_value,
            "catalysts": [c.to_dict() for c in self.catalysts],
            "staleness": [s.to_dict() for s in self.staleness],
            "warnings": self.warnings,
        }

    def explain(self) -> str:
        lines = [f"GMG SCORE: {self.value:.0f}/100" if self.value is not None
                 else "GMG SCORE: UNAVAILABLE"]
        lines.append("")
        lines.append(self.score.explain())
        for label, sub in (
            ("FUNDAMENTAL", self.fundamental), ("TECHNICAL", self.technical),
            ("QUANTITATIVE", self.quant), ("CATALYST", self.catalyst), ("RISK", self.risk),
        ):
            if sub is not None:
                lines.append("")
                lines.append(sub.explain())
        if self.warnings:
            lines.append("")
            lines.append("WARNINGS")
            lines.extend(f"  ⚠ {w}" for w in self.warnings)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Catalyst scoring
# ---------------------------------------------------------------------------
def score_catalysts(
    events: Sequence[CatalystEvent], *, as_of: date | None = None, horizon_days: int = 120
) -> tuple[ScoreResult, list[CatalystEvent]]:
    """Score recent corporate events by type, importance and recency.

    Recency decay matters: a merger announced last week is a live catalyst; the
    same announcement 18 months ago is history, not a reason to buy today.
    """
    as_of = as_of or date.today()
    relevant: list[CatalystEvent] = []
    contributions: list[float] = []

    for event in events:
        if event.event_date is None:
            continue
        age = (as_of - event.event_date).days
        if age < 0 or age > horizon_days:
            continue
        base = CATALYST_WEIGHTS.get(event.kind.upper(), 40.0)
        importance_factor = 0.6 + 0.1 * max(1, min(5, event.importance))
        recency = 1.0 - (age / horizon_days) * 0.6   # decays to 40% at the horizon
        contributions.append(base * importance_factor * recency)
        relevant.append(event)

    if not contributions:
        component = ScoreComponent(
            "corporate_events", 100.0, None,
            inputs={"events_considered": 0, "horizon_days": horizon_days},
            explanation="No corporate events or disclosures found within the horizon.",
        )
        return build_score("catalyst", [component]), []

    # The strongest event dominates; additional events add diminishing support.
    contributions.sort(reverse=True)
    total = contributions[0] + sum(c * 0.25 for c in contributions[1:3])
    value = max(0.0, min(100.0, total))

    component = ScoreComponent(
        "corporate_events", 100.0, value,
        inputs={
            "events_considered": len(relevant),
            "horizon_days": horizon_days,
            "strongest": relevant[0].kind if relevant else None,
        },
        explanation=(
            f"{len(relevant)} event(s) within {horizon_days} days, weighted by type, "
            "importance and recency. The strongest event dominates."
        ),
    )
    return build_score("catalyst", [component]), relevant


# ---------------------------------------------------------------------------
# Security-level risk scoring (higher score = LOWER risk)
# ---------------------------------------------------------------------------
def score_risk(inputs: RiskInputs) -> ScoreResult:
    """Score security-level risk. 100 = lowest risk, 0 = highest.

    Scored so that a *higher* number is always *better*, consistent with every
    other component, so the master weighting needs no sign handling.
    """
    components: list[ScoreComponent] = []

    components.append(ScoreComponent(
        "volatility_risk", 25,
        normalize_linear(inputs.volatility_annual, 0.80, 0.15),
        inputs={"annualised_volatility": _pct(inputs.volatility_annual)},
        explanation="Realised annualised volatility; 80%+ scores zero, 15% or less scores 100.",
    ))
    components.append(ScoreComponent(
        "drawdown_risk", 15,
        normalize_linear(inputs.max_drawdown_1y, -0.60, -0.05)
        if inputs.max_drawdown_1y is not None else None,
        inputs={"max_drawdown_1y": _pct(inputs.max_drawdown_1y)},
        explanation="Worst peak-to-trough decline over the last year.",
    ))
    components.append(ScoreComponent(
        "liquidity_risk", 20,
        # EGP 20m average daily turnover scores full marks; illiquidity is a
        # first-order risk on the EGX, where exiting a position can move it.
        normalize_linear(inputs.average_turnover, 200_000, 20_000_000),
        inputs={"average_daily_turnover_egp": inputs.average_turnover},
        explanation="Average daily traded value. Thin names are penalised heavily.",
    ))
    components.append(ScoreComponent(
        "leverage_risk", 20,
        _blend_scores([
            (normalize_linear(inputs.debt_to_equity, 3.0, 0.1), 0.5),
            (normalize_linear(inputs.net_debt_to_ebitda, 5.0, 0.0), 0.3),
            (normalize_linear(inputs.interest_coverage, 1.0, 10.0), 0.2),
        ]),
        inputs={
            "debt_to_equity": inputs.debt_to_equity,
            "net_debt_to_ebitda": inputs.net_debt_to_ebitda,
            "interest_coverage": inputs.interest_coverage,
        },
        explanation="Balance-sheet leverage and ability to service interest.",
    ))
    components.append(ScoreComponent(
        "data_risk", 10,
        normalize_linear(inputs.data_staleness_days, 540, 30)
        if inputs.data_staleness_days is not None else None,
        inputs={"data_age_days": inputs.data_staleness_days},
        explanation="Age of the underlying financial data. Stale data is itself a risk.",
    ))
    components.append(ScoreComponent(
        "event_risk", 10,
        # An imminent earnings release is gap risk, not an opportunity.
        normalize_linear(inputs.earnings_within_days, 0, 45)
        if inputs.earnings_within_days is not None else None,
        inputs={"days_to_earnings": inputs.earnings_within_days},
        explanation="Proximity to a scheduled earnings event (gap risk).",
    ))

    return build_score("risk", components)


def _blend_scores(parts: list[tuple[float | None, float]]) -> float | None:
    usable = [(s, w) for s, w in parts if s is not None]
    if not usable:
        return None
    total = sum(w for _, w in usable)
    return sum(s * w for s, w in usable) / total if total else None


def _pct(value: float | None) -> str | None:
    return None if value is None else f"{value:.1%}"


# ---------------------------------------------------------------------------
# Master score
# ---------------------------------------------------------------------------
def compute_alpha_score(
    ticker: str,
    *,
    fundamental: FundamentalSnapshot | None = None,
    technical: TechnicalSnapshot | None = None,
    quant: QuantSnapshot | None = None,
    catalyst_events: Sequence[CatalystEvent] = (),
    risk_inputs: RiskInputs | None = None,
    sentiment: float | None = None,
    as_of: date | None = None,
    weights: dict[str, float] | None = None,
    fundamentals_retrieved_at: datetime | None = None,
    prices_retrieved_at: datetime | None = None,
) -> AlphaScore:
    """Compute the GMG master score with a full audit trail."""
    as_of = as_of or date.today()
    cfg = weights or load_yaml_config("weights").get("alpha") or {}

    catalyst_result, relevant_events = score_catalysts(catalyst_events, as_of=as_of)
    risk_result = score_risk(risk_inputs or RiskInputs())

    fundamental_score = fundamental.score if fundamental and fundamental.score else None
    technical_score = technical.score if technical and technical.score else None
    quant_score = quant.score if quant and quant.score else None

    # "Quality" is lifted from the fundamental engine's own quality component so
    # it is weighted explicitly at the master level, as the brief specifies.
    quality_value: float | None = None
    if fundamental_score:
        for component in fundamental_score.components:
            if component.name == "quality" and component.available:
                quality_value = component.value
                break

    # Sentiment: lexicon score in [-1, 1] mapped onto 0-100.
    sentiment_score = None if sentiment is None else max(0.0, min(100.0, (sentiment + 1.0) * 50.0))

    components = [
        ScoreComponent(
            "fundamental", cfg.get("fundamental", 30),
            fundamental_score.value if fundamental_score else None,
            inputs={"coverage": round(fundamental_score.coverage, 2) if fundamental_score else None},
            explanation="Fundamental engine: valuation, quality, growth, profitability, balance sheet, cash flow.",
        ),
        ScoreComponent(
            "technical", cfg.get("technical", 20),
            technical_score.value if technical_score else None,
            inputs={"trend": technical.trend if technical else None},
            explanation="Technical engine: trend, momentum, RSI, MACD, volume, relative strength.",
        ),
        ScoreComponent(
            "quantitative", cfg.get("quantitative", 15),
            quant_score.value if quant_score else None,
            inputs={"universe_size": quant.universe_size if quant else None},
            explanation="Cross-sectional factor model against the EGX universe.",
        ),
        ScoreComponent(
            "catalysts", cfg.get("catalysts", 10), catalyst_result.value,
            inputs={"events": len(relevant_events)},
            explanation="Recent corporate events and disclosures, decayed by recency.",
        ),
        ScoreComponent(
            "quality", cfg.get("quality", 10), quality_value,
            inputs={"source": "fundamental.quality component"},
            explanation="Business quality: returns on capital, margins, cash conversion.",
        ),
        ScoreComponent(
            "risk", cfg.get("risk", 10), risk_result.value,
            inputs={"coverage": round(risk_result.coverage, 2)},
            explanation="Security-level risk, scored so that lower risk earns a higher number.",
        ),
        ScoreComponent(
            "sentiment", cfg.get("sentiment", 5), sentiment_score,
            inputs={"lexicon_sentiment": sentiment},
            explanation="Keyword-lexicon sentiment over recent news. A heuristic, not analysis.",
        ),
    ]

    result = build_score("egx_alpha", components)

    # --- Staleness and warnings ---------------------------------------------
    staleness: list[StalenessFinding] = []
    warnings: list[str] = []
    for dataset, retrieved in (
        ("fundamentals", fundamentals_retrieved_at), ("price", prices_retrieved_at)
    ):
        finding = assess_staleness(dataset, retrieved,
                                   datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc))
        if finding:
            staleness.append(finding)
            warnings.append(finding.message)

    if fundamental and fundamental.insufficient_data:
        warnings.append(
            "No financial statements available — the fundamental component is absent "
            "and its weight was redistributed."
        )
    if technical and technical.insufficient_data:
        warnings.append(
            f"Insufficient price history ({technical.bars_available} bars) for a "
            "technical read."
        )
    if result.coverage < 0.75 and result.available:
        warnings.append(
            f"Score built on {result.coverage:.0%} of component weight — treat with "
            f"{result.confidence.value} confidence."
        )
    # Staleness reduces the confidence attached to the score, as the brief requires.
    if staleness and result.available:
        worst = max(staleness, key=lambda s: s.age_days)
        if worst.severity == "critical" and result.confidence != Confidence.UNVERIFIED:
            result.confidence = Confidence.LOW

    return AlphaScore(
        ticker=ticker,
        as_of=as_of,
        score=result,
        fundamental=fundamental_score,
        technical=technical_score,
        quant=quant_score,
        catalyst=catalyst_result,
        risk=risk_result,
        sentiment_value=sentiment,
        quality_value=quality_value,
        catalysts=list(relevant_events),
        staleness=staleness,
        warnings=warnings,
    )
