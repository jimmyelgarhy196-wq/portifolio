"""Subscription lifecycle and entitlement.

The single question this module answers is: *may this user use premium
features right now?* :func:`user_is_entitled` is the only correct way to ask,
and it is enforced server-side on every protected route and API endpoint. The
frontend never decides access; it only reflects what the server already
determined.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.core.logging_config import get_logger
from backend.data.saas_models import (
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    User,
)

logger = get_logger(__name__)


def now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class PlanDefinition:
    code: str
    name: str
    price_egp: float
    interval: str
    trial_days: int
    features: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "price_egp": self.price_egp,
            "interval": self.interval, "trial_days": self.trial_days,
            "features": self.features,
        }


PLAN_FEATURES = [
    "EGX market dashboard with indices, gainers, losers and most active",
    "Market data with source, timestamp and delay always shown",
    "GMG AI research: thesis, bull/base/bear case, catalysts and risks",
    "Fundamental analysis with sourced figures and a 0-100 score",
    "Technical analysis with indicators and a 0-100 score",
    "Valuation tools including a DCF calculator",
    "Advanced stock screener with saved screens",
    "Unlimited watchlists",
    "Portfolio tracking with P/L and allocation",
    "Weekly GMG EGX Intelligence report",
    "Price and indicator alerts by email",
]


def current_plan() -> PlanDefinition:
    s = get_settings()
    return PlanDefinition(
        code=s.plan_code, name=s.plan_name, price_egp=s.plan_price_egp,
        interval=s.plan_interval, trial_days=s.trial_days, features=list(PLAN_FEATURES),
    )


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def get_subscription(session: Session, user: User) -> Subscription | None:
    """The user's most relevant subscription: an entitling one, else the newest."""
    subs = session.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
    ).scalars().all()
    if not subs:
        return None
    for sub in subs:
        if sub.is_entitled():
            return sub
    return subs[0]


def user_is_entitled(session: Session, user: User | None) -> bool:
    """The authoritative access check. Server-side only.

    Admins are entitled so they can support customers without buying a plan.
    """
    if user is None or not user.is_active:
        return False
    if user.is_admin:
        return True
    sub = get_subscription(session, user)
    return bool(sub and sub.is_entitled())


def entitlement_state(session: Session, user: User | None) -> dict[str, Any]:
    """Everything a template needs to explain access, in one call."""
    plan = current_plan()
    if user is None:
        return {"entitled": False, "reason": "anonymous", "subscription": None,
                "plan": plan.to_dict(), "status": None, "days_remaining": None}
    if user.is_admin:
        return {"entitled": True, "reason": "admin", "subscription": None,
                "plan": plan.to_dict(), "status": "ADMIN", "days_remaining": None}

    sub = get_subscription(session, user)
    if sub is None:
        return {"entitled": False, "reason": "no_subscription", "subscription": None,
                "plan": plan.to_dict(), "status": None, "days_remaining": None}
    entitled = sub.is_entitled()
    return {
        "entitled": entitled,
        "reason": "active" if entitled else f"status_{sub.status.lower()}",
        "subscription": sub.to_dict(), "plan": plan.to_dict(),
        "status": sub.status, "days_remaining": sub.days_remaining,
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def start_trial(session: Session, user: User) -> Subscription:
    """Open a trial. Idempotent: an existing entitling subscription is returned."""
    existing = get_subscription(session, user)
    if existing is not None and existing.is_entitled():
        return existing

    plan = current_plan()
    trial_end = now() + timedelta(days=plan.trial_days)
    sub = Subscription(
        user_id=user.id, plan_code=plan.code, plan_name=plan.name,
        status=SubscriptionStatus.TRIAL.value, price_egp=plan.price_egp,
        interval=plan.interval, trial_ends_at=_naive(trial_end),
        current_period_start=_naive(now()), current_period_end=_naive(trial_end),
        provider=get_settings().payment_provider,
    )
    session.add(sub)
    session.flush()
    logger.info("Trial started for %s until %s", user.email, trial_end.date())
    return sub


def activate_subscription(
    session: Session, subscription: Subscription, *,
    period_days: int = 30, payment: Payment | None = None,
) -> Subscription:
    """Mark a subscription paid and extend its period.

    Called only when a payment is genuinely confirmed — by a verified gateway
    webhook, or by an administrator recording an out-of-band transfer.
    """
    start = now()
    # Extend from the existing end when renewing early, so no paid time is lost.
    if subscription.current_period_end is not None:
        existing_end = subscription.current_period_end
        if existing_end.tzinfo is None:
            existing_end = existing_end.replace(tzinfo=timezone.utc)
        if existing_end > start:
            start = existing_end

    subscription.status = SubscriptionStatus.ACTIVE.value
    subscription.current_period_start = _naive(start)
    subscription.current_period_end = _naive(start + timedelta(days=period_days))
    subscription.cancelled_at = None
    subscription.ended_at = None
    if payment is not None:
        payment.status = PaymentStatus.SUCCEEDED.value
        payment.settled_at = _naive(now())
        payment.subscription_id = subscription.id
        payment.period_start = subscription.current_period_start.date()
        payment.period_end = subscription.current_period_end.date()
    session.flush()
    logger.info(
        "Subscription %s activated until %s",
        subscription.id, subscription.current_period_end,
    )
    return subscription


def cancel_subscription(
    session: Session, subscription: Subscription, *, reason: str | None = None
) -> Subscription:
    """Cancel at period end. Access continues for time already paid for."""
    subscription.status = SubscriptionStatus.CANCELLED.value
    subscription.cancelled_at = _naive(now())
    subscription.cancel_reason = (reason or "").strip()[:500] or None
    session.flush()
    return subscription


def resume_subscription(session: Session, subscription: Subscription) -> Subscription:
    """Undo a cancellation while the paid period is still running."""
    if subscription.is_entitled():
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.cancelled_at = None
        subscription.cancel_reason = None
    session.flush()
    return subscription


def mark_past_due(session: Session, subscription: Subscription, reason: str) -> Subscription:
    subscription.status = SubscriptionStatus.PAST_DUE.value
    subscription.meta = {**(subscription.meta or {}), "past_due_reason": reason}
    session.flush()
    return subscription


def expire_due_subscriptions(session: Session, *, at: datetime | None = None) -> int:
    """Move subscriptions whose period has ended into EXPIRED. Returns the count.

    Run by the scheduler. Access is already denied by ``is_entitled`` the moment
    the period ends — this only makes the stored status match reality.
    """
    cutoff = _naive(at or now())
    rows = session.execute(
        select(Subscription).where(
            Subscription.status.in_([
                SubscriptionStatus.TRIAL.value, SubscriptionStatus.ACTIVE.value,
                SubscriptionStatus.CANCELLED.value, SubscriptionStatus.PAST_DUE.value,
            ])
        )
    ).scalars().all()

    expired = 0
    for sub in rows:
        if sub.is_entitled(at=at or now()):
            continue
        sub.status = SubscriptionStatus.EXPIRED.value
        sub.ended_at = cutoff
        expired += 1
    if expired:
        session.flush()
        logger.info("Expired %d subscription(s)", expired)
    return expired


# ---------------------------------------------------------------------------
# Metrics for the admin panel
# ---------------------------------------------------------------------------
def billing_metrics(session: Session) -> dict[str, Any]:
    """Subscriber counts, MRR and churn, computed from stored rows only."""
    subs = session.execute(select(Subscription)).scalars().all()
    by_status: dict[str, int] = {}
    for sub in subs:
        by_status[sub.status] = by_status.get(sub.status, 0) + 1

    paying = [
        s for s in subs
        if s.status == SubscriptionStatus.ACTIVE.value and s.is_entitled()
    ]
    trialing = [
        s for s in subs
        if s.status == SubscriptionStatus.TRIAL.value and s.is_entitled()
    ]
    # MRR counts paying subscriptions only. Trials are not revenue.
    mrr = sum(s.price_egp for s in paying)

    settled = session.execute(
        select(Payment).where(Payment.status == PaymentStatus.SUCCEEDED.value)
    ).scalars().all()
    revenue_total = sum(p.amount_egp for p in settled)

    thirty_days_ago = _naive(now() - timedelta(days=30))
    cancelled_30d = sum(
        1 for s in subs
        if s.cancelled_at is not None and s.cancelled_at >= thirty_days_ago
    )
    active_start = len(paying) + cancelled_30d
    churn_rate = (cancelled_30d / active_start) if active_start else 0.0

    pending = session.scalar(
        select(func.count()).select_from(Payment)
        .where(Payment.status == PaymentStatus.PENDING.value)
    ) or 0

    return {
        "subscriptions_total": len(subs),
        "by_status": by_status,
        "paying": len(paying),
        "trialing": len(trialing),
        "mrr_egp": round(mrr, 2),
        "arr_egp": round(mrr * 12, 2),
        "revenue_recorded_egp": round(revenue_total, 2),
        "payments_pending": pending,
        "cancelled_30d": cancelled_30d,
        "churn_rate_30d": round(churn_rate, 4),
        "note": (
            "Revenue counts only payments explicitly confirmed as received. "
            "No payment gateway is connected, so this figure reflects manually "
            "confirmed transfers only."
        ) if not get_settings().payments_enabled else None,
    }
