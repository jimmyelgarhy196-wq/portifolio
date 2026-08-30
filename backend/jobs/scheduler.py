"""Scheduler for automated runs.

Disabled by default (``EGX_SCHEDULER_ENABLED=false``). When enabled it runs the
weekly pipeline on a configurable cron and takes a daily portfolio snapshot so
volatility, beta and drawdown have the history they need.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from backend.core.config import get_settings
from backend.core.logging_config import get_logger

logger = get_logger(__name__)

_scheduler: Any = None


def _parse_cron(expression: str) -> dict[str, str]:
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError(
            f"Invalid cron expression {expression!r}: expected 5 fields "
            "(minute hour day month day_of_week)."
        )
    minute, hour, day, month, day_of_week = parts
    return {
        "minute": minute, "hour": hour, "day": day,
        "month": month, "day_of_week": day_of_week,
    }


def weekly_job() -> None:
    from backend.jobs.weekly import run_weekly_pipeline

    settings = get_settings()
    logger.info("Scheduled weekly pipeline starting")
    result = run_weekly_pipeline(
        acknowledge_synthetic=settings.allow_synthetic_data
    )
    logger.info("Scheduled weekly pipeline finished\n%s", result.render())


def daily_snapshot_job() -> None:
    """Persist a daily portfolio valuation. Return-based risk depends on this."""
    from sqlalchemy import select

    from backend.core.database import session_scope
    from backend.data.models import Portfolio
    from backend.portfolio.paper_trading import snapshot_portfolio

    with session_scope() as session:
        portfolio = session.scalar(select(Portfolio).order_by(Portfolio.portfolio_id))
        if portfolio is None:
            return
        snapshot = snapshot_portfolio(session, portfolio, as_of=date.today())
        logger.info(
            "Daily snapshot: %s %s %.2f",
            snapshot.as_of, portfolio.currency, snapshot.total_value,
        )


def start_scheduler() -> Any | None:
    """Start the background scheduler if enabled. Returns it, or None."""
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info(
            "Scheduler disabled. Set EGX_SCHEDULER_ENABLED=true to enable automated runs."
        )
        return None
    if _scheduler is not None:
        return _scheduler

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:  # pragma: no cover
        logger.error("APScheduler is not installed; automated runs are unavailable.")
        return None

    scheduler = BackgroundScheduler(timezone=settings.timezone)
    try:
        fields = _parse_cron(settings.weekly_cron)
    except ValueError as exc:
        logger.error("%s Scheduler not started.", exc)
        return None

    scheduler.add_job(
        weekly_job, CronTrigger(timezone=settings.timezone, **fields),
        id="weekly_research", replace_existing=True, max_instances=1,
    )
    scheduler.add_job(
        daily_snapshot_job,
        CronTrigger(hour=16, minute=30, day_of_week="sun,mon,tue,wed,thu",
                    timezone=settings.timezone),
        id="daily_snapshot", replace_existing=True, max_instances=1,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "Scheduler started (timezone %s): weekly '%s', daily snapshot 16:30 Sun-Thu",
        settings.timezone, settings.weekly_cron,
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")


def scheduler_status() -> dict[str, Any]:
    settings = get_settings()
    if _scheduler is None:
        return {
            "enabled": settings.scheduler_enabled,
            "running": False,
            "weekly_cron": settings.weekly_cron,
            "timezone": settings.timezone,
            "jobs": [],
        }
    return {
        "enabled": True,
        "running": _scheduler.running,
        "weekly_cron": settings.weekly_cron,
        "timezone": settings.timezone,
        "jobs": [
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in _scheduler.get_jobs()
        ],
    }
