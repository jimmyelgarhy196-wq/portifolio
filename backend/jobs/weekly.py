"""The weekly research pipeline.

Runs the eleven steps the brief specifies, in order. Each step is isolated: a
failure is recorded and the run continues, because a news-feed outage should not
prevent the committee report from being produced.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.core.database import session_scope
from backend.core.logging_config import EVENT_REPORT, get_logger, log_event

logger = get_logger(__name__)


@dataclass
class StepResult:
    name: str
    status: str = "OK"          # OK | SKIPPED | FAILED
    detail: str = ""
    duration_seconds: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "status": self.status, "detail": self.detail,
            "duration_seconds": round(self.duration_seconds, 2), "data": self.data,
        }


@dataclass
class WeeklyRunResult:
    as_of: date
    steps: list[StepResult] = field(default_factory=list)
    report_id: int | None = None

    @property
    def failed(self) -> list[StepResult]:
        return [s for s in self.steps if s.status == "FAILED"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "report_id": self.report_id,
            "failed": len(self.failed),
            "steps": [s.to_dict() for s in self.steps],
        }

    def render(self) -> str:
        lines = [f"EGX ALPHA weekly run — {self.as_of.isoformat()}", ""]
        for index, step in enumerate(self.steps, start=1):
            icon = {"OK": "✓", "SKIPPED": "–", "FAILED": "✗"}.get(step.status, "?")
            lines.append(
                f"  {icon} {index:>2}. {step.name:<34} {step.duration_seconds:>6.1f}s  "
                f"{step.detail}"
            )
        if self.report_id:
            lines += ["", f"Report stored with id {self.report_id}."]
        if self.failed:
            lines += ["", f"{len(self.failed)} step(s) failed — see the detail above."]
        return "\n".join(lines)


def _step(result: WeeklyRunResult, name: str, fn) -> StepResult:
    """Run one pipeline step, isolating its failure from the rest of the run."""
    started = time.monotonic()
    step = StepResult(name=name)
    try:
        outcome = fn()
        if isinstance(outcome, StepResult):
            step = outcome
            step.name = name
        elif isinstance(outcome, str):
            step.detail = outcome
        elif isinstance(outcome, dict):
            step.data = outcome
            step.detail = outcome.get("detail", "")
    except Exception as exc:  # noqa: BLE001 - a failed step must not abort the run
        step.status = "FAILED"
        step.detail = f"{exc.__class__.__name__}: {exc}"
        logger.exception("Weekly step %r failed", name)
    step.duration_seconds = time.monotonic() - started
    result.steps.append(step)
    return step


def run_weekly_pipeline(
    *,
    as_of: date | None = None,
    index: str = "egx30",
    tickers: Sequence[str] | None = None,
    skip_ingestion: bool = False,
    research_limit: int | None = 25,
    acknowledge_synthetic: bool = False,
) -> WeeklyRunResult:
    """Execute the full weekly research and reporting pipeline."""
    as_of = as_of or date.today()
    settings = get_settings()
    result = WeeklyRunResult(as_of=as_of)

    log_event(logger, EVENT_REPORT, f"Weekly pipeline starting for {as_of}", as_of=as_of.isoformat())

    # --- 1-4: data refresh ---------------------------------------------------
    if skip_ingestion:
        for name in ("Update market data", "Update financial data",
                     "Update news", "Update disclosures"):
            result.steps.append(StepResult(name, "SKIPPED", "Ingestion skipped by request."))
    else:
        from backend.data.ingestion import (
            ingest_disclosures, ingest_fundamentals, ingest_news, ingest_prices,
        )

        def _ingest(fn, **kwargs):
            with session_scope() as session:
                summary = fn(session, **kwargs)
                failed = len(summary.failures)
                status = "OK" if not failed else ("FAILED" if not summary.inserted else "OK")
                return StepResult(
                    name="", status=status,
                    detail=(
                        f"+{summary.inserted} new, ~{summary.updated} updated, "
                        f"{failed} provider failure(s)"
                    ),
                    data=summary.to_dict(),
                )

        _step(result, "Update market data",
              lambda: _ingest(ingest_prices, index=index, lookback_days=400))
        _step(result, "Update financial data",
              lambda: _ingest(ingest_fundamentals, index=index))
        _step(result, "Update news", lambda: _ingest(ingest_news))
        _step(result, "Update disclosures", lambda: _ingest(ingest_disclosures))

    # --- 5-9: analysis, research, theses -------------------------------------
    def _recalculate() -> StepResult:
        from backend.analytics.service import analyze_all

        with session_scope() as session:
            results = analyze_all(session, index=index, as_of=as_of, persist=True)
            scored = sum(1 for r in results.values() if r.alpha.value is not None)
            return StepResult(
                name="", detail=f"{scored}/{len(results)} names scored",
                data={"scored": scored, "total": len(results)},
            )

    _step(result, "Recalculate scores", _recalculate)

    def _research() -> StepResult:
        from backend.research.pipeline import run_research

        with session_scope() as session:
            run = run_research(
                session, tickers=tickers, index=index, as_of=as_of,
                persist=True, limit=research_limit,
            )
            return StepResult(
                name="",
                status="OK" if not run.errors else "OK",
                detail=(
                    f"{len(run.results)} researched, {len(run.new_theses)} new theses, "
                    f"{len(run.updated_theses)} updated, "
                    f"{'LLM' if run.used_llm else 'deterministic'} narrative"
                ),
                data={
                    "researched": len(run.results),
                    "new_theses": len(run.new_theses),
                    "updated_theses": len(run.updated_theses),
                    "llm_calls": run.llm_calls,
                    "errors": run.errors[:5],
                },
            )

    _step(result, "Re-run research and update theses", _research)

    def _review_positions() -> StepResult:
        from sqlalchemy import select

        from backend.data.models import Portfolio, Position
        from backend.portfolio.paper_trading import mark_to_market, snapshot_portfolio
        from backend.research.thesis import check_invalidation, get_active_theses

        with session_scope() as session:
            portfolio = session.scalar(select(Portfolio).order_by(Portfolio.portfolio_id))
            if portfolio is None:
                return StepResult(name="", status="SKIPPED", detail="No portfolio exists.")
            state = mark_to_market(session, portfolio, as_of=as_of)
            snapshot_portfolio(session, portfolio, as_of=as_of)

            invalidated: list[str] = []
            held = {p.ticker for p in state["positions"]}
            for thesis in get_active_theses(session):
                if thesis.ticker not in held:
                    continue
                price = next(
                    (p.current_price for p in state["positions"] if p.ticker == thesis.ticker),
                    None,
                )
                if check_invalidation(session, thesis, price):
                    invalidated.append(thesis.ticker)
            return StepResult(
                name="",
                detail=(
                    f"{len(state['positions'])} position(s) revalued; "
                    f"{len(invalidated)} thesis invalidation(s)"
                ),
                data={"positions": len(state["positions"]), "invalidated": invalidated},
            )

    _step(result, "Review current positions", _review_positions)

    def _opportunities() -> StepResult:
        from sqlalchemy import func, select

        from backend.data.models import ScoreHistory

        with session_scope() as session:
            count = session.scalar(
                select(func.count()).select_from(ScoreHistory).where(
                    ScoreHistory.as_of == as_of, ScoreHistory.alpha_score >= 70
                )
            ) or 0
            return StepResult(
                name="", detail=f"{count} name(s) scoring 70 or above",
                data={"high_scoring": count},
            )

    _step(result, "Identify new opportunities", _opportunities)

    def _alerts() -> StepResult:
        from backend.reports.alerts import dispatch_notifications, run_all_checks

        with session_scope() as session:
            alerts = run_all_checks(session, as_of=as_of)
            notification = dispatch_notifications(session)
            detail = f"{len(alerts)} alert(s) raised"
            if notification.skipped_reason:
                detail += "; notifications off"
            elif notification.sent:
                detail += f"; {notification.sent} notification(s) sent"
            return StepResult(
                name="", detail=detail,
                data={"alerts": len(alerts), "notifications": notification.to_dict()},
            )

    _step(result, "Run alert checks", _alerts)

    def _grade() -> StepResult:
        from backend.reports.evaluation import grade_recommendations

        with session_scope() as session:
            graded = grade_recommendations(session, as_of=as_of)
            return StepResult(
                name="", detail=f"{graded} recommendation(s) graded", data={"graded": graded}
            )

    _step(result, "Grade past recommendations", _grade)

    # --- 10-11: report -------------------------------------------------------
    def _report() -> StepResult:
        from sqlalchemy import desc, select

        from backend.data.models import Report
        from backend.reports.weekly import SyntheticDataRefused, generate_weekly_report

        with session_scope() as session:
            try:
                generate_weekly_report(
                    session, as_of=as_of,
                    acknowledge_synthetic=acknowledge_synthetic, persist=True,
                )
            except SyntheticDataRefused as exc:
                return StepResult(name="", status="SKIPPED", detail=str(exc)[:200])
            record = session.scalar(select(Report).order_by(desc(Report.id)))
            result.report_id = record.id if record else None
            return StepResult(
                name="", detail=f"Report stored with id {result.report_id}",
                data={"report_id": result.report_id},
            )

    _step(result, "Generate and store weekly report", _report)

    log_event(
        logger, EVENT_REPORT,
        f"Weekly pipeline complete: {len(result.steps)} steps, {len(result.failed)} failed",
        as_of=as_of.isoformat(), report_id=result.report_id,
    )
    return result
