"""Model evaluation — how accurate has the system actually been?

Grades stored recommendations against subsequent price action. Because
``recommendations`` is append-only, this measures what the system said at the
time, not a reconstruction of what it would say now.

Every recommendation is graded against the benchmark over the same window, so a
call is credited with the excess return it produced rather than with a rising
market.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.core.data_quality import safe_div
from backend.core.logging_config import get_logger
from backend.data.models import PriceBar, Recommendation

logger = get_logger(__name__)

#: How long a recommendation is given before it is graded, by strategy.
EVALUATION_HORIZONS = {
    "fundamental_long": 180,
    "special_situations": 120,
    "technical_swing": 45,
    "bearish_short": 90,
    None: 90,
}


def _price_on_or_before(session: Session, ticker: str, on: date) -> float | None:
    bar = session.scalar(
        select(PriceBar)
        .where(PriceBar.ticker == ticker.upper(), PriceBar.timestamp <= on)
        .order_by(PriceBar.timestamp.desc())
    )
    if bar is None:
        return None
    return bar.close if bar.close is not None else bar.adjusted_close


@dataclass
class GroupPerformance:
    name: str
    count: int = 0
    graded: int = 0
    wins: int = 0
    losses: int = 0
    average_return: float | None = None
    average_excess: float | None = None
    median_return: float | None = None
    best: float | None = None
    worst: float | None = None
    average_holding_days: float | None = None
    hit_rate_vs_benchmark: float | None = None

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return self.wins / decided if decided else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": self.count,
            "graded": self.graded,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": None if self.win_rate is None else round(self.win_rate, 3),
            "average_return": _r(self.average_return),
            "average_excess": _r(self.average_excess),
            "median_return": _r(self.median_return),
            "best": _r(self.best),
            "worst": _r(self.worst),
            "average_holding_days": (
                None if self.average_holding_days is None
                else round(self.average_holding_days, 1)
            ),
            "hit_rate_vs_benchmark": _r(self.hit_rate_vs_benchmark, 3),
        }


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


@dataclass
class EvaluationReport:
    as_of: date
    total_recommendations: int = 0
    graded: int = 0
    pending: int = 0
    overall: GroupPerformance = field(default_factory=lambda: GroupPerformance("overall"))
    by_strategy: list[GroupPerformance] = field(default_factory=list)
    by_action: list[GroupPerformance] = field(default_factory=list)
    by_sector: list[GroupPerformance] = field(default_factory=list)
    by_conviction: list[GroupPerformance] = field(default_factory=list)
    by_score_range: list[GroupPerformance] = field(default_factory=list)
    by_holding_period: list[GroupPerformance] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "total_recommendations": self.total_recommendations,
            "graded": self.graded,
            "pending": self.pending,
            "overall": self.overall.to_dict(),
            "by_strategy": [g.to_dict() for g in self.by_strategy],
            "by_action": [g.to_dict() for g in self.by_action],
            "by_sector": [g.to_dict() for g in self.by_sector],
            "by_conviction": [g.to_dict() for g in self.by_conviction],
            "by_score_range": [g.to_dict() for g in self.by_score_range],
            "by_holding_period": [g.to_dict() for g in self.by_holding_period],
            "notes": self.notes,
        }


def grade_recommendations(
    session: Session, *, as_of: date | None = None, force: bool = False
) -> int:
    """Grade every recommendation whose horizon has elapsed. Returns the count.

    A recommendation is graded once and then left alone, so the record of what
    the system predicted — and how it turned out — is stable.
    """
    as_of = as_of or date.today()
    settings = get_settings()
    stmt = select(Recommendation)
    if not force:
        stmt = stmt.where(Recommendation.outcome_status == "OPEN")
    rows = session.execute(stmt).scalars().all()

    graded = 0
    for reco in rows:
        created = reco.created_at.date() if reco.created_at else None
        if created is None:
            continue
        horizon = EVALUATION_HORIZONS.get(reco.strategy, EVALUATION_HORIZONS[None])
        target_date = created + timedelta(days=horizon)
        if target_date > as_of and not force:
            continue

        evaluation_date = min(target_date, as_of)
        entry = reco.price_at_reco or _price_on_or_before(session, reco.ticker, created)
        exit_price = _price_on_or_before(session, reco.ticker, evaluation_date)
        if entry is None or exit_price is None or entry <= 0:
            reco.outcome_status = "NO_DATA"
            continue

        raw_return = (exit_price - entry) / entry
        # A SELL/short call is judged on the decline it anticipated.
        if reco.direction == "SHORT" or reco.action == "SELL":
            raw_return = -raw_return

        benchmark_start = _price_on_or_before(session, settings.benchmark_ticker, created)
        benchmark_end = _price_on_or_before(session, settings.benchmark_ticker, evaluation_date)
        benchmark_return = None
        if benchmark_start and benchmark_end and benchmark_start > 0:
            benchmark_return = (benchmark_end - benchmark_start) / benchmark_start
            if reco.direction == "SHORT" or reco.action == "SELL":
                benchmark_return = -benchmark_return

        reco.outcome_price = exit_price
        reco.outcome_date = datetime(
            evaluation_date.year, evaluation_date.month, evaluation_date.day,
            tzinfo=timezone.utc,
        ).replace(tzinfo=None)
        reco.realized_return = raw_return
        reco.benchmark_return = benchmark_return
        reco.holding_days = (evaluation_date - created).days
        reco.outcome_status = "GRADED"
        graded += 1

    session.flush()
    if graded:
        logger.info("Graded %d recommendation(s) as of %s", graded, as_of)
    return graded


def _summarise(name: str, rows: Sequence[Recommendation]) -> GroupPerformance:
    group = GroupPerformance(name=name, count=len(rows))
    graded = [r for r in rows if r.outcome_status == "GRADED" and r.realized_return is not None]
    group.graded = len(graded)
    if not graded:
        return group

    returns = sorted(r.realized_return for r in graded)
    group.wins = sum(1 for r in returns if r > 0)
    group.losses = sum(1 for r in returns if r < 0)
    group.average_return = sum(returns) / len(returns)
    mid = len(returns) // 2
    group.median_return = (
        returns[mid] if len(returns) % 2 else (returns[mid - 1] + returns[mid]) / 2
    )
    group.best, group.worst = returns[-1], returns[0]

    excess = [
        r.realized_return - r.benchmark_return
        for r in graded if r.benchmark_return is not None
    ]
    if excess:
        group.average_excess = sum(excess) / len(excess)
        group.hit_rate_vs_benchmark = sum(1 for e in excess if e > 0) / len(excess)

    holds = [r.holding_days for r in graded if r.holding_days is not None]
    if holds:
        group.average_holding_days = sum(holds) / len(holds)
    return group


def _bucket(rows: Sequence[Recommendation], key_fn, label_fn=None) -> list[GroupPerformance]:
    buckets: dict[Any, list[Recommendation]] = {}
    for row in rows:
        key = key_fn(row)
        if key is None:
            continue
        buckets.setdefault(key, []).append(row)
    out = [
        _summarise(label_fn(k) if label_fn else str(k), v)
        for k, v in buckets.items()
    ]
    out.sort(key=lambda g: (g.average_return if g.average_return is not None else -99), reverse=True)
    return out


def evaluate_model(
    session: Session, *, as_of: date | None = None, grade_first: bool = True
) -> EvaluationReport:
    """Full evaluation of the system's historical accuracy."""
    as_of = as_of or date.today()
    if grade_first:
        grade_recommendations(session, as_of=as_of)

    rows = list(session.execute(select(Recommendation)).scalars().all())
    report = EvaluationReport(as_of=as_of, total_recommendations=len(rows))
    report.graded = sum(1 for r in rows if r.outcome_status == "GRADED")
    report.pending = sum(1 for r in rows if r.outcome_status == "OPEN")

    if not rows:
        report.notes.append(
            "No recommendations have been recorded yet. Run the research pipeline to "
            "start building a track record."
        )
        return report
    if report.graded == 0:
        report.notes.append(
            f"{report.pending} recommendation(s) recorded, but none has reached its "
            "evaluation horizon yet. Accuracy cannot be assessed until they do — "
            "reporting a win rate now would be measuring noise."
        )

    report.overall = _summarise("overall", rows)
    report.by_strategy = _bucket(rows, lambda r: r.strategy or "unclassified")
    report.by_action = _bucket(rows, lambda r: r.action)
    report.by_sector = _bucket(rows, lambda r: r.sector or "Unclassified")
    report.by_conviction = _bucket(rows, _conviction_bucket)
    report.by_score_range = _bucket(rows, _score_bucket)
    report.by_holding_period = _bucket(rows, _holding_bucket)

    no_data = sum(1 for r in rows if r.outcome_status == "NO_DATA")
    if no_data:
        report.notes.append(
            f"{no_data} recommendation(s) could not be graded because price history "
            "was missing for the evaluation window."
        )
    return report


def _conviction_bucket(reco: Recommendation) -> str | None:
    if reco.conviction is None:
        return None
    if reco.conviction >= 8:
        return "8.0-10.0 (high)"
    if reco.conviction >= 6:
        return "6.0-7.9 (moderate)"
    if reco.conviction >= 4:
        return "4.0-5.9 (low)"
    return "0.0-3.9 (minimal)"


def _score_bucket(reco: Recommendation) -> str | None:
    if reco.alpha_score is None:
        return None
    if reco.alpha_score >= 80:
        return "80-100"
    if reco.alpha_score >= 70:
        return "70-79"
    if reco.alpha_score >= 60:
        return "60-69"
    if reco.alpha_score >= 50:
        return "50-59"
    return "below 50"


def _holding_bucket(reco: Recommendation) -> str | None:
    if reco.holding_days is None:
        return None
    if reco.holding_days <= 30:
        return "0-30 days"
    if reco.holding_days <= 90:
        return "31-90 days"
    if reco.holding_days <= 180:
        return "91-180 days"
    return "over 180 days"
