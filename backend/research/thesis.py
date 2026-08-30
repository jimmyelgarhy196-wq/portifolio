"""Investment thesis engine.

A thesis is durable. It is created once per (ticker, direction) and **updated**
each week rather than replaced, with every prior state kept as an immutable
:class:`ThesisVersion`. That is what makes "what changed since last week?" — a
mandatory question in the brief — answerable from the record rather than from
recollection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.analytics.service import StockAnalysis
from backend.core.data_quality import Claim
from backend.core.logging_config import EVENT_RESEARCH_RUN, get_logger, log_event
from backend.data.models import Recommendation, ResearchThesis, ThesisVersion
from backend.portfolio.sizing import compute_risk_reward
from backend.research.agents import AgentOutput, PortfolioDecision
from backend.research.evidence import EvidencePack

logger = get_logger(__name__)

STRATEGY_LABELS = {
    "fundamental_long": "Core Fundamental Long",
    "technical_swing": "Technical / Swing",
    "special_situations": "Special Situations",
    "bearish_short": "Bearish / Paper Short",
}

HOLDING_PERIODS = {
    "fundamental_long": "6-36+ months",
    "technical_swing": "days to several months",
    "special_situations": "event-dependent, typically 1-9 months",
    "bearish_short": "variable",
}

#: Fields whose change between versions is worth reporting in the weekly diff.
TRACKED_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("alpha_score", "EGX ALPHA score", "score"),
    ("fundamental_score", "Fundamental score", "score"),
    ("technical_score", "Technical score", "score"),
    ("quant_score", "Quant score", "score"),
    ("catalyst_score", "Catalyst score", "score"),
    ("risk_score", "Risk score", "score"),
    ("conviction", "Conviction", "conviction"),
    ("entry_price", "Entry", "price"),
    ("target_price", "Target", "price"),
    ("invalidation_price", "Invalidation", "price"),
    ("status", "Status", "text"),
    ("direction", "Direction", "text"),
    ("strategy", "Strategy", "text"),
)


def next_reference(session: Session) -> str:
    """Sequential human-facing reference, e.g. ``EGX-00047``."""
    count = session.scalar(select(func.count()).select_from(ResearchThesis)) or 0
    return f"EGX-{count + 1:05d}"


def classify_strategy(analysis: StockAnalysis, decision: PortfolioDecision) -> str:
    """Pick the strategy a thesis belongs to, from the evidence.

    Order matters: a live corporate action defines a special situation
    regardless of what the fundamentals look like.
    """
    alpha = analysis.alpha
    event_kinds = {e.kind.upper() for e in alpha.catalysts}
    if event_kinds & {"M&A", "BUYBACK", "CAPITAL_ACTION", "RESTRUCTURING"}:
        return "special_situations"

    fundamental = alpha.fundamental.value if alpha.fundamental else None
    technical = alpha.technical.value if alpha.technical else None

    if decision.action == "SELL" or (fundamental is not None and fundamental < 35):
        return "bearish_short"
    if technical is not None and fundamental is not None:
        return "technical_swing" if technical > fundamental + 12 else "fundamental_long"
    if fundamental is not None:
        return "fundamental_long"
    return "technical_swing" if technical is not None else "fundamental_long"


@dataclass
class ThesisLevels:
    """Entry, target and invalidation, each derived from a named input."""

    entry: float | None = None
    target: float | None = None
    invalidation: float | None = None
    rationale: dict[str, str] = field(default_factory=dict)

    @property
    def expected_return(self) -> float | None:
        if self.entry and self.target and self.entry > 0:
            return (self.target - self.entry) / self.entry
        return None

    @property
    def expected_downside(self) -> float | None:
        if self.entry and self.invalidation and self.entry > 0:
            return (self.invalidation - self.entry) / self.entry
        return None


def derive_levels(analysis: StockAnalysis, direction: str = "LONG") -> ThesisLevels:
    """Derive levels from technical structure and volatility.

    Every level cites the input that produced it. Where the structure supplies
    nothing, an ATR-based level is used and labelled as such — never a round
    number chosen because it looks like a target.
    """
    levels = ThesisLevels()
    technical = analysis.technical
    price = analysis.price_series.last_close
    if price is None:
        return levels

    levels.entry = price
    levels.rationale["entry"] = "Last traded close."
    if technical is None or technical.insufficient_data:
        levels.rationale["target"] = "No technical structure available to derive a target."
        levels.rationale["invalidation"] = "No technical structure available."
        return levels

    atr = technical.atr14
    is_long = direction.upper() == "LONG"

    if is_long:
        # Target: nearest resistance above, else an ATR-derived extension.
        above = [r for r in technical.resistance_levels if r > price]
        if above:
            levels.target = min(above)
            levels.rationale["target"] = (
                f"Nearest identified resistance at {levels.target:,.2f}."
            )
        elif atr:
            levels.target = price + atr * 4.0
            levels.rationale["target"] = (
                f"No resistance identified above the current price; target set at "
                f"4x ATR ({atr:,.2f}) above entry."
            )
        # Invalidation: nearest support below, else an ATR-derived stop.
        below = [s for s in technical.support_levels if s < price]
        if below:
            levels.invalidation = max(below)
            levels.rationale["invalidation"] = (
                f"Nearest identified support at {levels.invalidation:,.2f}; a decisive "
                "break says the technical premise is wrong."
            )
        elif atr:
            levels.invalidation = price - atr * 2.5
            levels.rationale["invalidation"] = (
                f"No support identified below; invalidation set at 2.5x ATR "
                f"({atr:,.2f}) below entry."
            )
    else:
        below = [s for s in technical.support_levels if s < price]
        if below:
            levels.target = max(below)
            levels.rationale["target"] = f"Nearest support at {levels.target:,.2f}."
        elif atr:
            levels.target = price - atr * 4.0
            levels.rationale["target"] = f"4x ATR ({atr:,.2f}) below entry."
        above = [r for r in technical.resistance_levels if r > price]
        if above:
            levels.invalidation = min(above)
            levels.rationale["invalidation"] = (
                f"Nearest resistance at {levels.invalidation:,.2f}; reclaiming it "
                "invalidates the bearish premise."
            )
        elif atr:
            levels.invalidation = price + atr * 2.5
            levels.rationale["invalidation"] = f"2.5x ATR ({atr:,.2f}) above entry."

    return levels


def extract_bullets(output: AgentOutput | None, claims: Sequence[Claim], limit: int = 6) -> list[str]:
    if output is None:
        return []
    wanted = set(claims)
    return [s.text for s in output.statements if s.claim in wanted][:limit]


@dataclass
class ThesisBundle:
    """Everything produced by a research run on one name."""

    thesis: ResearchThesis
    recommendation: Recommendation | None
    decision: PortfolioDecision
    agent_outputs: dict[str, AgentOutput]
    change_summary: str | None
    is_new: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis": self.thesis.to_dict(),
            "recommendation": self.recommendation.to_dict() if self.recommendation else None,
            "decision": self.decision.to_dict(),
            "agents": {k: v.to_dict() for k, v in self.agent_outputs.items()},
            "change_summary": self.change_summary,
            "is_new": self.is_new,
        }


def upsert_thesis(
    session: Session,
    analysis: StockAnalysis,
    pack: EvidencePack,
    decision: PortfolioDecision,
    agent_outputs: dict[str, AgentOutput],
    *,
    as_of: date | None = None,
    record_recommendation: bool = True,
) -> ThesisBundle:
    """Create or update the thesis for this name, versioning the prior state."""
    as_of = as_of or analysis.as_of
    alpha = analysis.alpha
    direction = "SHORT" if decision.action == "SELL" else "LONG"
    strategy = classify_strategy(analysis, decision)
    if strategy == "bearish_short":
        direction = "SHORT"

    levels = derive_levels(analysis, direction)
    risk_reward = compute_risk_reward(
        levels.entry, levels.target, levels.invalidation, direction
    )

    existing = session.scalar(
        select(ResearchThesis).where(
            ResearchThesis.ticker == analysis.ticker,
            ResearchThesis.direction == direction,
            ResearchThesis.status.in_(("ACTIVE", "WATCH", "UNDER_REVIEW")),
        )
    )

    fundamental_out = agent_outputs.get("fundamental")
    technical_out = agent_outputs.get("technical")
    event_out = agent_outputs.get("event")
    bear_out = agent_outputs.get("bear")

    bull_case = "\n".join(
        extract_bullets(fundamental_out, (Claim.FACT, Claim.INFERENCE), 8)
        + extract_bullets(technical_out, (Claim.INFERENCE,), 3)
    )
    bear_case = "\n".join(
        extract_bullets(bear_out, (Claim.FACT, Claim.INFERENCE, Claim.OPINION), 10)
    )

    payload: dict[str, Any] = {
        "ticker": analysis.ticker,
        "direction": direction,
        "strategy": strategy,
        "entry_price": levels.entry,
        "target_price": levels.target,
        "invalidation_price": levels.invalidation,
        "expected_return": levels.expected_return,
        "expected_downside": levels.expected_downside,
        "risk_reward": risk_reward,
        "expected_holding_period": HOLDING_PERIODS.get(strategy, "unspecified"),
        "conviction": decision.conviction,
        "fundamental_score": alpha.fundamental.value if alpha.fundamental else None,
        "technical_score": alpha.technical.value if alpha.technical else None,
        "quant_score": alpha.quant.value if alpha.quant else None,
        "catalyst_score": alpha.catalyst.value if alpha.catalyst else None,
        "risk_score": alpha.risk.value if alpha.risk else None,
        "alpha_score": alpha.value,
        "thesis_text": decision.rationale,
        "bull_case": bull_case,
        "bear_case": bear_case,
        "catalysts": [e.to_dict() for e in alpha.catalysts],
        "risks": extract_bullets(bear_out, (Claim.INFERENCE, Claim.OPINION), 8),
        "invalidation_conditions": decision.exit_conditions,
        "data_sources": pack.sources,
        "statements": [
            {"agent": name, **s.to_dict()}
            for name, output in agent_outputs.items()
            for s in output.statements
        ],
        "status": _status_for(decision.action),
        "generated_by": (
            "llm" if any(o.generated_by == "llm" for o in agent_outputs.values())
            else "deterministic"
        ),
    }

    change_summary: str | None = None
    is_new = existing is None

    if existing is None:
        thesis = ResearchThesis(reference=next_reference(session), version=1, **payload)
        session.add(thesis)
        session.flush()
        change_summary = f"Thesis opened at an EGX ALPHA score of {alpha.value:.0f}." if alpha.value is not None else "Thesis opened."
    else:
        thesis = existing
        before = {field: getattr(thesis, field) for field, _l, _k in TRACKED_FIELDS}
        # Snapshot the prior state before mutating — this is the audit trail.
        session.add(ThesisVersion(
            thesis_id=thesis.thesis_id, version=thesis.version,
            snapshot=thesis.to_dict(),
            change_summary=f"State prior to the {as_of.isoformat()} update.",
        ))
        for key, value in payload.items():
            setattr(thesis, key, value)
        thesis.version += 1
        thesis.updated_at = datetime.now(timezone.utc)
        after = {field: getattr(thesis, field) for field, _l, _k in TRACKED_FIELDS}
        change_summary = summarise_changes(before, after)
        session.flush()

    recommendation: Recommendation | None = None
    if record_recommendation:
        recommendation = Recommendation(
            ticker=analysis.ticker,
            thesis_id=thesis.thesis_id,
            action=decision.action,
            direction=direction,
            strategy=strategy,
            sector=analysis.company.sector if analysis.company else None,
            price_at_reco=levels.entry,
            target_price=levels.target,
            invalidation_price=levels.invalidation,
            conviction=decision.conviction,
            alpha_score=alpha.value,
            expected_return=levels.expected_return,
            expected_holding_period=HOLDING_PERIODS.get(strategy),
            rationale=decision.rationale[:4000] if decision.rationale else None,
            created_at=datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc).replace(tzinfo=None),
        )
        session.add(recommendation)
        session.flush()

    log_event(
        logger, EVENT_RESEARCH_RUN,
        f"{'Opened' if is_new else 'Updated'} thesis {thesis.reference} for {analysis.ticker}",
        ticker=analysis.ticker, reference=thesis.reference, action=decision.action,
        conviction=decision.conviction, version=thesis.version,
        generated_by=payload["generated_by"],
    )
    return ThesisBundle(
        thesis=thesis, recommendation=recommendation, decision=decision,
        agent_outputs=agent_outputs, change_summary=change_summary, is_new=is_new,
    )


def _status_for(action: str) -> str:
    return {
        "BUY": "ACTIVE", "HOLD": "ACTIVE", "WATCH": "WATCH", "SELL": "EXIT_PROPOSED",
    }.get(action, "ACTIVE")


def summarise_changes(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Human-readable diff between two thesis states."""
    lines: list[str] = []
    for field, label, kind in TRACKED_FIELDS:
        old, new = before.get(field), after.get(field)
        if old is None and new is None:
            continue
        if kind in ("score", "conviction", "price"):
            if old is None or new is None:
                if old != new:
                    lines.append(f"{label}: {_fmt_value(old, kind)} → {_fmt_value(new, kind)}")
                continue
            threshold = {"score": 1.0, "conviction": 0.3, "price": 0.005}[kind]
            delta = abs(new - old)
            relative = delta / abs(old) if (kind == "price" and old) else delta
            if relative >= threshold:
                arrow = "↑" if new > old else "↓"
                lines.append(
                    f"{label}: {_fmt_value(old, kind)} → {_fmt_value(new, kind)} "
                    f"{arrow}{_fmt_delta(new - old, kind)}"
                )
        elif old != new:
            lines.append(f"{label}: {old} → {new}")
    return "\n".join(lines) if lines else "No material change since the previous review."


def _fmt_value(value: Any, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "score":
        return f"{value:.0f}"
    if kind == "conviction":
        return f"{value:.1f}"
    if kind == "price":
        return f"{value:,.2f}"
    return str(value)


def _fmt_delta(delta: float, kind: str) -> str:
    if kind == "score":
        return f"{abs(delta):.0f}"
    if kind == "conviction":
        return f"{abs(delta):.1f}"
    return f"{abs(delta):,.2f}"


def get_active_theses(session: Session, *, ticker: str | None = None) -> list[ResearchThesis]:
    stmt = select(ResearchThesis).where(
        ResearchThesis.status.in_(("ACTIVE", "WATCH", "UNDER_REVIEW", "EXIT_PROPOSED"))
    )
    if ticker:
        stmt = stmt.where(ResearchThesis.ticker == ticker.upper())
    return list(session.execute(
        stmt.order_by(ResearchThesis.alpha_score.desc().nullslast())
    ).scalars().all())


def check_invalidation(
    session: Session, thesis: ResearchThesis, current_price: float | None
) -> dict[str, Any] | None:
    """Has price breached the invalidation level? Returns a description or None."""
    if current_price is None or thesis.invalidation_price is None:
        return None
    breached = (
        current_price <= thesis.invalidation_price if thesis.direction == "LONG"
        else current_price >= thesis.invalidation_price
    )
    if not breached:
        return None
    return {
        "thesis_id": thesis.thesis_id,
        "reference": thesis.reference,
        "ticker": thesis.ticker,
        "direction": thesis.direction,
        "invalidation_price": thesis.invalidation_price,
        "current_price": current_price,
        "message": (
            f"{thesis.ticker} {thesis.direction} thesis {thesis.reference} is invalidated: "
            f"price {current_price:,.2f} has breached the invalidation level of "
            f"{thesis.invalidation_price:,.2f}."
        ),
    }


def render_thesis(thesis: ResearchThesis) -> str:
    """Render a thesis in the document format the brief specifies."""
    def num(value: Any, fmt: str = ",.2f", suffix: str = "") -> str:
        return f"{value:{fmt}}{suffix}" if value is not None else "—"

    lines = [
        f"THESIS #{thesis.reference}",
        "",
        f"Ticker: {thesis.ticker}",
        f"Direction: {thesis.direction}",
        f"Strategy: {STRATEGY_LABELS.get(thesis.strategy, thesis.strategy)}",
        "",
        f"Entry:          {num(thesis.entry_price)}",
        f"Target:         {num(thesis.target_price)}",
        f"Invalidation:   {num(thesis.invalidation_price)}",
        "",
        f"Expected Return:    {num(thesis.expected_return, '+.1%')}",
        f"Expected Downside:  {num(thesis.expected_downside, '+.1%')}",
        f"Risk/Reward:        {num(thesis.risk_reward, '.2f', ':1')}",
        "",
        f"Holding Period: {thesis.expected_holding_period or '—'}",
        f"Conviction:     {num(thesis.conviction, '.1f', '/10')}",
        "",
        f"Fundamental Score: {num(thesis.fundamental_score, '.0f')}",
        f"Technical Score:   {num(thesis.technical_score, '.0f')}",
        f"Quant Score:       {num(thesis.quant_score, '.0f')}",
        f"Catalyst Score:    {num(thesis.catalyst_score, '.0f')}",
        f"Risk Score:        {num(thesis.risk_score, '.0f')}",
        f"EGX ALPHA SCORE:   {num(thesis.alpha_score, '.0f')}",
        "",
        "INVESTMENT THESIS",
        "",
        thesis.thesis_text or "—",
        "",
        "BULL CASE",
        "",
        thesis.bull_case or "—",
        "",
        "BEAR CASE",
        "",
        thesis.bear_case or "—",
        "",
        "CATALYSTS",
        "",
    ]
    catalysts = thesis.catalysts or []
    lines += [
        f"  [{c.get('date') or 'undated'}] {c.get('kind')}: {c.get('title')}"
        for c in catalysts
    ] or ["  None identified in the available disclosures."]
    lines += ["", "RISKS", ""]
    lines += [f"  - {r}" for r in (thesis.risks or [])] or ["  —"]
    lines += ["", "INVALIDATION CONDITIONS", ""]
    lines += [f"  - {c}" for c in (thesis.invalidation_conditions or [])] or ["  —"]
    lines += ["", "DATA SOURCES", ""]
    lines += [f"  - {s}" for s in (thesis.data_sources or [])] or ["  —"]
    lines += [
        "",
        f"Version {thesis.version} · generated by {thesis.generated_by} · "
        f"updated {thesis.updated_at.date().isoformat() if thesis.updated_at else '—'}",
    ]
    return "\n".join(lines)
