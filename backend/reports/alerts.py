"""Alert generation and notification dispatch.

Alerts are created from real state changes — price targets reached, invalidation
levels breached, risk limits exceeded, scores moving materially, new material
disclosures. Notifications are **not sent** unless explicitly configured, per
the brief.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.core.logging_config import EVENT_ALERT, get_logger, log_event
from backend.data.models import (
    Alert,
    Disclosure,
    Portfolio,
    Position,
    PriceBar,
    ResearchThesis,
    ScoreHistory,
    WatchlistItem,
)

logger = get_logger(__name__)

ALERT_TYPES = (
    "PRICE_TARGET", "INVALIDATION", "BREAKOUT", "VOLUME_SPIKE", "EARNINGS",
    "DISCLOSURE", "NEWS", "SCORE_CHANGE", "THESIS_INVALIDATED", "RISK_LIMIT",
    "HIGH_CONVICTION", "REPORT_READY",
)

#: Score move that counts as material. Below this it is noise.
SCORE_CHANGE_THRESHOLD = 8.0


def _latest_price(session: Session, ticker: str, as_of: date | None = None) -> float | None:
    stmt = select(PriceBar).where(PriceBar.ticker == ticker.upper())
    if as_of:
        stmt = stmt.where(PriceBar.timestamp <= as_of)
    bar = session.scalar(stmt.order_by(PriceBar.timestamp.desc()))
    if bar is None:
        return None
    return bar.close if bar.close is not None else bar.adjusted_close


def _exists(session: Session, alert_type: str, ticker: str | None, title: str,
            within_days: int = 7) -> bool:
    """Avoid re-raising the same alert every run."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=within_days)
    return session.scalar(
        select(Alert).where(
            Alert.alert_type == alert_type,
            Alert.ticker == ticker,
            Alert.title == title,
            Alert.created_at >= since,
        )
    ) is not None


def create_alert(
    session: Session,
    *,
    alert_type: str,
    title: str,
    message: str,
    ticker: str | None = None,
    severity: str = "info",
    payload: dict[str, Any] | None = None,
    dedupe_days: int = 7,
) -> Alert | None:
    if _exists(session, alert_type, ticker, title, dedupe_days):
        return None
    alert = Alert(
        ticker=ticker, alert_type=alert_type, severity=severity,
        title=title, message=message, payload=payload or {}, status="NEW",
    )
    session.add(alert)
    session.flush()
    log_event(
        logger, EVENT_ALERT, title,
        alert_type=alert_type, ticker=ticker, severity=severity,
    )
    return alert


def check_price_targets(session: Session, *, as_of: date | None = None) -> list[Alert]:
    """Theses whose target has been reached or invalidation breached."""
    out: list[Alert] = []
    theses = session.execute(
        select(ResearchThesis).where(
            ResearchThesis.status.in_(("ACTIVE", "WATCH", "EXIT_PROPOSED"))
        )
    ).scalars().all()

    for thesis in theses:
        price = _latest_price(session, thesis.ticker, as_of)
        if price is None:
            continue

        if thesis.target_price:
            reached = (
                price >= thesis.target_price if thesis.direction == "LONG"
                else price <= thesis.target_price
            )
            if reached:
                alert = create_alert(
                    session, alert_type="PRICE_TARGET", ticker=thesis.ticker,
                    severity="info",
                    title=f"{thesis.ticker} reached its price target",
                    message=(
                        f"{thesis.ticker} is trading at {price:,.2f} against a target of "
                        f"{thesis.target_price:,.2f} in thesis {thesis.reference}. "
                        "Review whether to take profit or raise the target."
                    ),
                    payload={"thesis_id": thesis.thesis_id, "price": price,
                             "target": thesis.target_price},
                )
                if alert:
                    out.append(alert)

        if thesis.invalidation_price:
            breached = (
                price <= thesis.invalidation_price if thesis.direction == "LONG"
                else price >= thesis.invalidation_price
            )
            if breached:
                alert = create_alert(
                    session, alert_type="THESIS_INVALIDATED", ticker=thesis.ticker,
                    severity="critical",
                    title=f"{thesis.ticker} thesis invalidated",
                    message=(
                        f"{thesis.ticker} is trading at {price:,.2f}, through the "
                        f"invalidation level of {thesis.invalidation_price:,.2f} set in "
                        f"thesis {thesis.reference}. The premise of this position no "
                        "longer holds — exit or rewrite the thesis."
                    ),
                    payload={"thesis_id": thesis.thesis_id, "price": price,
                             "invalidation": thesis.invalidation_price},
                )
                if alert:
                    out.append(alert)
                    thesis.status = "INVALIDATED"
    return out


def check_score_changes(
    session: Session, *, as_of: date | None = None, lookback_days: int = 14
) -> list[Alert]:
    """Material week-over-week score moves."""
    as_of = as_of or date.today()
    out: list[Alert] = []
    tickers = session.execute(
        select(ScoreHistory.ticker).distinct()
    ).scalars().all()

    for ticker in tickers:
        current = session.scalar(
            select(ScoreHistory)
            .where(ScoreHistory.ticker == ticker, ScoreHistory.as_of <= as_of)
            .order_by(ScoreHistory.as_of.desc())
        )
        if current is None or current.alpha_score is None:
            continue
        previous = session.scalar(
            select(ScoreHistory)
            .where(
                ScoreHistory.ticker == ticker,
                ScoreHistory.as_of < current.as_of,
                ScoreHistory.as_of >= current.as_of - timedelta(days=lookback_days),
            )
            .order_by(ScoreHistory.as_of.desc())
        )
        if previous is None or previous.alpha_score is None:
            continue
        delta = current.alpha_score - previous.alpha_score
        if abs(delta) < SCORE_CHANGE_THRESHOLD:
            continue
        direction = "rose" if delta > 0 else "fell"
        alert = create_alert(
            session, alert_type="SCORE_CHANGE", ticker=ticker,
            severity="warning" if delta < 0 else "info",
            title=f"{ticker} score {direction} {abs(delta):.0f} points",
            message=(
                f"The EGX ALPHA score for {ticker} moved from "
                f"{previous.alpha_score:.0f} to {current.alpha_score:.0f} between "
                f"{previous.as_of} and {current.as_of}."
            ),
            payload={"from": previous.alpha_score, "to": current.alpha_score,
                     "delta": delta},
            dedupe_days=5,
        )
        if alert:
            out.append(alert)
    return out


def check_high_conviction(
    session: Session, *, as_of: date | None = None, threshold: float = 75.0
) -> list[Alert]:
    """New high-scoring opportunities not already held."""
    as_of = as_of or date.today()
    held = set(session.execute(select(Position.ticker)).scalars().all())
    out: list[Alert] = []

    rows = session.execute(
        select(ScoreHistory)
        .where(ScoreHistory.as_of == as_of, ScoreHistory.alpha_score >= threshold)
        .order_by(ScoreHistory.alpha_score.desc())
    ).scalars().all()

    for row in rows:
        if row.ticker in held:
            continue
        alert = create_alert(
            session, alert_type="HIGH_CONVICTION", ticker=row.ticker, severity="info",
            title=f"{row.ticker} scores {row.alpha_score:.0f} and is not held",
            message=(
                f"{row.ticker} has an EGX ALPHA score of {row.alpha_score:.0f} "
                f"(confidence {row.confidence}) and is not in the portfolio. "
                "Review the thesis."
            ),
            payload={"alpha_score": row.alpha_score},
        )
        if alert:
            out.append(alert)
    return out


def check_disclosures(
    session: Session, *, as_of: date | None = None, days: int = 3
) -> list[Alert]:
    """Material disclosures on held or watched names."""
    as_of = as_of or date.today()
    since = as_of - timedelta(days=days)
    watched = set(session.execute(select(Position.ticker)).scalars().all())
    watched |= set(session.execute(select(WatchlistItem.ticker)).scalars().all())
    if not watched:
        return []

    rows = session.execute(
        select(Disclosure).where(
            Disclosure.ticker.in_(watched),
            Disclosure.date >= since,
            Disclosure.date <= as_of,
            Disclosure.importance >= 4,
        )
    ).scalars().all()

    out: list[Alert] = []
    for row in rows:
        alert = create_alert(
            session, alert_type="DISCLOSURE", ticker=row.ticker, severity="warning",
            title=f"{row.ticker}: {row.disclosure_type or 'disclosure'}",
            message=f"{row.title}\n\nSource: {row.source}",
            payload={"disclosure_id": row.disclosure_id, "url": row.url},
        )
        if alert:
            out.append(alert)
    return out


def check_risk_limits(session: Session, *, as_of: date | None = None) -> list[Alert]:
    """Portfolio risk-limit breaches, raised as alerts."""
    from backend.portfolio.risk import analyze_risk

    portfolio = session.scalar(select(Portfolio).order_by(Portfolio.portfolio_id))
    if portfolio is None:
        return []
    report = analyze_risk(session, portfolio, as_of=as_of)
    out: list[Alert] = []
    for warning in report.warnings:
        if warning.severity == "info":
            continue
        alert = create_alert(
            session, alert_type="RISK_LIMIT", ticker=warning.ticker,
            severity=warning.severity, title=warning.title,
            message=warning.message,
            payload={"code": warning.code, "current": warning.current,
                     "limit": warning.limit},
            dedupe_days=3,
        )
        if alert:
            out.append(alert)
    return out


def run_all_checks(session: Session, *, as_of: date | None = None) -> list[Alert]:
    """Run every alert check. Used by the weekly job."""
    alerts: list[Alert] = []
    alerts += check_price_targets(session, as_of=as_of)
    alerts += check_score_changes(session, as_of=as_of)
    alerts += check_high_conviction(session, as_of=as_of)
    alerts += check_disclosures(session, as_of=as_of)
    alerts += check_risk_limits(session, as_of=as_of)
    session.flush()
    return alerts


# ---------------------------------------------------------------------------
# Notification dispatch
# ---------------------------------------------------------------------------
@dataclass
class NotificationResult:
    attempted: int = 0
    sent: int = 0
    skipped_reason: str | None = None
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted, "sent": self.sent,
            "skipped_reason": self.skipped_reason, "errors": self.errors,
        }


def dispatch_notifications(
    session: Session, alerts: Sequence[Alert] | None = None
) -> NotificationResult:
    """Send notifications for unnotified alerts.

    Disabled by default. Nothing leaves this machine unless the user has
    explicitly configured a channel.
    """
    settings = get_settings()
    result = NotificationResult()

    if not settings.notifications_enabled:
        result.skipped_reason = (
            "Notifications are disabled. Set EGX_NOTIFICATIONS_ENABLED=true and "
            "configure a channel to enable them."
        )
        return result

    pending = list(alerts) if alerts is not None else list(session.execute(
        select(Alert).where(Alert.notified.is_(False), Alert.status == "NEW")
    ).scalars().all())
    result.attempted = len(pending)
    if not pending:
        return result

    channels: list[str] = []
    if settings.notify_webhook_url:
        channels.append("webhook")
    if settings.notify_email_to and settings.smtp_host:
        channels.append("email")
    if not channels:
        result.skipped_reason = (
            "Notifications are enabled but no channel is configured. Set "
            "EGX_NOTIFY_WEBHOOK_URL, or EGX_NOTIFY_EMAIL_TO with SMTP settings."
        )
        return result

    for alert in pending:
        delivered = False
        if "webhook" in channels:
            try:
                import httpx

                httpx.post(
                    settings.notify_webhook_url,
                    json={
                        "source": "EGX ALPHA",
                        "severity": alert.severity,
                        "title": alert.title,
                        "message": alert.message,
                        "ticker": alert.ticker,
                        "type": alert.alert_type,
                    },
                    timeout=settings.http_timeout_seconds,
                )
                delivered = True
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"webhook: {exc}")

        if "email" in channels and not delivered:
            try:
                _send_email(settings, alert)
                delivered = True
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"email: {exc}")

        if delivered:
            alert.notified = True
            result.sent += 1

    session.flush()
    return result


def _send_email(settings: Any, alert: Alert) -> None:
    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = f"[EGX ALPHA] {alert.title}"
    message["From"] = settings.smtp_user or "egx-alpha@localhost"
    message["To"] = settings.notify_email_to
    message.set_content(
        f"{alert.title}\n\n{alert.message}\n\n"
        f"Severity: {alert.severity}\nType: {alert.alert_type}\n"
        f"Ticker: {alert.ticker or '—'}\n\n"
        "This is an automated message from your EGX ALPHA research terminal. "
        "It is research output, not investment advice."
    )
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)
