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


def user_alerts_job() -> None:
    """Evaluate subscriber alerts and email the ones that fired.

    Runs through the trading session rather than once a day, because an alert
    that arrives after the close is not an alert. It never fires on
    demonstration data — see :mod:`backend.notify.user_alerts`.
    """
    from backend.core.database import session_scope
    from backend.notify.user_alerts import run_user_alerts

    with session_scope() as session:
        results = run_user_alerts(session)
    triggered = sum(1 for r in results if r.triggered)
    if triggered:
        logger.info("Alert sweep: %d of %d alerts fired", triggered, len(results))


def expire_subscriptions_job() -> None:
    """Retire subscriptions whose paid period has ended.

    Entitlement already checks the period end on every request, so this only
    tidies the stored status; access is never granted by a stale row.
    """
    from backend.billing.subscriptions import expire_due_subscriptions
    from backend.core.database import session_scope

    with session_scope() as session:
        count = expire_due_subscriptions(session)
    if count:
        logger.info("Expired %d subscription(s) past their paid period", count)


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
    # Every 15 minutes through the EGX session, Sunday to Thursday.
    scheduler.add_job(
        user_alerts_job,
        CronTrigger(minute="*/15", hour="10-14", day_of_week="sun,mon,tue,wed,thu",
                    timezone=settings.timezone),
        id="user_alerts", replace_existing=True, max_instances=1,
    )
    scheduler.add_job(
        expire_subscriptions_job,
        CronTrigger(hour=3, minute=0, timezone=settings.timezone),
        id="expire_subscriptions", replace_existing=True, max_instances=1,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "Scheduler started (timezone %s): weekly '%s', daily snapshot 16:30, "
        "alert sweep every 15m during the session, subscription expiry 03:00",
        settings.timezone, settings.weekly_cron,
    )
    return scheduler


def run_forever() -> int:
    """Run the scheduler as its own process, with no HTTP server.

    Used by the ``scheduler`` container. The web workers keep
    ``EGX_SCHEDULER_ENABLED=false`` so the weekly report is not generated once
    per worker — this process is the single place jobs run.
    """
    import signal
    import threading

    from backend.core.database import init_database

    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.error(
            "EGX_SCHEDULER_ENABLED is false, so there is nothing to run. "
            "Set it to true on the scheduler process only."
        )
        return 1

    init_database()
    if start_scheduler() is None:
        return 1

    stop = threading.Event()

    def handle(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s, shutting the scheduler down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    logger.info("Scheduler process ready. Jobs: %s",
                ", ".join(j["id"] for j in scheduler_status()["jobs"]))
    stop.wait()
    shutdown_scheduler()
    return 0


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
