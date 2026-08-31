"""Administration panel.

Every route requires an administrator, checked server-side. Two design points:

* Activating a subscription from here is a deliberate, audited act by a named
  administrator recording an out-of-band payment. It is the only way money can
  be marked received when no gateway is connected, and every use is written to
  the audit log with who did it.
* The panel never displays a password hash, a session token, or a token digest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from backend.accounts.service import audit, set_account_status, set_role
from backend.api.auth_deps import Viewer, client_ip, enforce_csrf, require_admin
from backend.api.deps import gmg_context, render
from backend.api.routes_auth import flash_from
from backend.billing.payments import payment_status
from backend.billing.subscriptions import (
    activate_subscription,
    billing_metrics,
    expire_due_subscriptions,
    get_subscription,
    start_trial,
)
from backend.core.config import get_settings
from backend.core.database import get_db
from backend.data.models import Company, PriceBar, Report, ScoreHistory
from backend.data.saas_models import (
    AccountStatus,
    AuditLog,
    DataSourceRecord,
    EmailLog,
    Payment,
    PaymentStatus,
    Quote,
    Subscription,
    User,
    UserRole,
)
from backend.market.quotes import provider_chain, refresh_quotes
from backend.notify.email_service import email_status, send_email

router = APIRouter(prefix="/admin")


def _ctx(request: Request, db: Session, **extra: Any) -> dict[str, Any]:
    context = gmg_context(request, db, active="admin", **extra)
    context.setdefault("flash", flash_from(request))
    return context


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request, db: Session = Depends(get_db), viewer: Viewer = Depends(require_admin)
):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    week_ago = now - timedelta(days=7)

    counts = {
        "users": db.scalar(select(func.count()).select_from(User)) or 0,
        "users_7d": db.scalar(
            select(func.count()).select_from(User).where(User.created_at >= week_ago)) or 0,
        "verified": db.scalar(
            select(func.count()).select_from(User)
            .where(User.email_verified_at.isnot(None))) or 0,
        "companies": db.scalar(select(func.count()).select_from(Company)) or 0,
        "price_bars": db.scalar(select(func.count()).select_from(PriceBar)) or 0,
        "quotes": db.scalar(select(func.count()).select_from(Quote)) or 0,
        "demo_quotes": db.scalar(
            select(func.count()).select_from(Quote).where(Quote.is_demo.is_(True))) or 0,
        "scores": db.scalar(select(func.count()).select_from(ScoreHistory)) or 0,
        "reports": db.scalar(select(func.count()).select_from(Report)) or 0,
        "emails_7d": db.scalar(
            select(func.count()).select_from(EmailLog)
            .where(EmailLog.created_at >= week_ago)) or 0,
        "emails_failed_7d": db.scalar(
            select(func.count()).select_from(EmailLog)
            .where(EmailLog.created_at >= week_ago, EmailLog.status == "FAILED")) or 0,
    }

    recent_users = list(db.execute(
        select(User).order_by(desc(User.created_at)).limit(10)).scalars().all())
    recent_audit = list(db.execute(
        select(AuditLog).order_by(desc(AuditLog.created_at)).limit(20)).scalars().all())
    sources = list(db.execute(
        select(DataSourceRecord).order_by(DataSourceRecord.name)).scalars().all())

    return render(request, "gmg/admin/dashboard.html", _ctx(
        request, db, counts=counts, metrics=billing_metrics(db),
        recent_users=recent_users, recent_audit=recent_audit, sources=sources,
        providers=[
            {"name": p.display_name, "is_demo": p.is_demo, "available": p.is_available(),
             "delay": p.delayed_minutes, "note": p.status_note()}
            for p in provider_chain(db)
        ],
        email=email_status(), payments=payment_status(),
    ))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@router.get("/users", response_class=HTMLResponse)
def users(
    request: Request, q: str = Query(""), status: str = Query(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_admin),
):
    stmt = select(User).order_by(desc(User.created_at)).limit(200)
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(User.email).like(needle))
    if status:
        stmt = stmt.where(User.status == status)
    rows = list(db.execute(stmt).scalars().all())
    subscriptions = {
        s.user_id: s for s in db.execute(select(Subscription)).scalars().all()
    }
    return render(request, "gmg/admin/users.html", _ctx(
        request, db, users=rows, subscriptions=subscriptions, q=q, status=status,
        statuses=[s.value for s in AccountStatus], roles=[r.value for r in UserRole],
    ))


@router.get("/users/{user_id}", response_class=HTMLResponse)
def user_detail(
    request: Request, user_id: int, db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user.")
    payments = list(db.execute(
        select(Payment).where(Payment.user_id == user.id)
        .order_by(desc(Payment.created_at)).limit(30)).scalars().all())
    emails = list(db.execute(
        select(EmailLog).where(EmailLog.to_email == user.email)
        .order_by(desc(EmailLog.created_at)).limit(30)).scalars().all())
    events = list(db.execute(
        select(AuditLog).where(AuditLog.user_id == user.id)
        .order_by(desc(AuditLog.created_at)).limit(40)).scalars().all())
    return render(request, "gmg/admin/user_detail.html", _ctx(
        request, db, subject=user, subscription=get_subscription(db, user),
        payments=payments, emails=emails, events=events,
        statuses=[s.value for s in AccountStatus], roles=[r.value for r in UserRole],
    ))


@router.post("/users/{user_id}/status")
def change_status(
    request: Request, user_id: int, csrf_token: str = Form(""), status: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_admin),
):
    enforce_csrf(request, csrf_token)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user.")
    if user.id == viewer.user.id:
        return RedirectResponse(f"/admin/users/{user_id}?msg=self_change_blocked", status_code=303)
    try:
        target_status = AccountStatus(status)
    except ValueError:
        raise HTTPException(400, "Unknown account status.")
    set_account_status(db, user, target_status, actor=viewer.user, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/users/{user_id}/role")
def change_role(
    request: Request, user_id: int, csrf_token: str = Form(""), role: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_admin),
):
    enforce_csrf(request, csrf_token)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user.")
    if user.id == viewer.user.id:
        # An admin cannot demote themselves; that is how a system ends up with none.
        return RedirectResponse(f"/admin/users/{user_id}?msg=self_change_blocked", status_code=303)
    try:
        target_role = UserRole(role)
    except ValueError:
        raise HTTPException(400, "Unknown role.")
    set_role(db, user, target_role, actor=viewer.user, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


# ---------------------------------------------------------------------------
# Subscriptions and payments
# ---------------------------------------------------------------------------
@router.get("/subscriptions", response_class=HTMLResponse)
def subscriptions(
    request: Request, db: Session = Depends(get_db), viewer: Viewer = Depends(require_admin)
):
    rows = list(db.execute(
        select(Subscription).order_by(desc(Subscription.created_at)).limit(200)
    ).scalars().all())
    users = {u.id: u for u in db.execute(select(User)).scalars().all()}
    pending = list(db.execute(
        select(Payment).where(Payment.status == PaymentStatus.PENDING.value)
        .order_by(desc(Payment.created_at)).limit(50)
    ).scalars().all())
    return render(request, "gmg/admin/subscriptions.html", _ctx(
        request, db, subscriptions=rows, users=users, pending=pending,
        metrics=billing_metrics(db), payments=payment_status(),
    ))


@router.post("/payments/{payment_id}/confirm")
def confirm_payment(
    request: Request, payment_id: int, csrf_token: str = Form(""),
    reference: str = Form(""), db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_admin),
):
    """Record that an out-of-band payment genuinely arrived.

    This is the manual equivalent of a verified gateway webhook. It is audited
    with the administrator's identity and the reference they entered, because
    it is the one place a human asserts that money was received.
    """
    enforce_csrf(request, csrf_token)
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(404, "No such payment.")
    if payment.status == PaymentStatus.SUCCEEDED.value:
        return RedirectResponse("/admin/subscriptions?msg=already_confirmed", status_code=303)

    user = db.get(User, payment.user_id)
    if user is None:
        raise HTTPException(404, "No such user.")

    subscription = get_subscription(db, user) or start_trial(db, user)
    payment.external_id = (reference or "").strip()[:128] or None
    activate_subscription(db, subscription, payment=payment)
    audit(db, "payment_confirmed_by_admin", user=user, actor_email=viewer.user.email,
          ip=client_ip(request), payment_id=payment.id, reference=payment.external_id,
          amount_egp=payment.amount_egp)
    send_email(db, to=user.email, template="subscription_confirmed",
               name=user.display_name, ends_on=subscription.current_period_end)
    db.commit()
    return RedirectResponse("/admin/subscriptions?msg=payment_confirmed", status_code=303)


@router.post("/subscriptions/expire")
def expire_subscriptions(
    request: Request, csrf_token: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_admin),
):
    enforce_csrf(request, csrf_token)
    count = expire_due_subscriptions(db)
    audit(db, "subscriptions_expired", actor_email=viewer.user.email,
          ip=client_ip(request), count=count)
    db.commit()
    return RedirectResponse("/admin/subscriptions", status_code=303)


# ---------------------------------------------------------------------------
# Data and operations
# ---------------------------------------------------------------------------
@router.get("/data", response_class=HTMLResponse)
def data_panel(
    request: Request, db: Session = Depends(get_db), viewer: Viewer = Depends(require_admin)
):
    from backend.data.providers.registry import provider_status

    bar_sources = list(db.execute(
        select(PriceBar.source, func.count(), func.max(PriceBar.timestamp))
        .group_by(PriceBar.source)
    ).all())
    quotes = list(db.execute(select(Quote).order_by(Quote.ticker).limit(200)).scalars().all())
    return render(request, "gmg/admin/data.html", _ctx(
        request, db, bar_sources=bar_sources, quotes=quotes,
        sources=list(db.execute(select(DataSourceRecord)).scalars().all()),
        market_providers=provider_status(),
        providers=[
            {"name": p.display_name, "is_demo": p.is_demo, "available": p.is_available(),
             "delay": p.delayed_minutes, "note": p.status_note()}
            for p in provider_chain(db)
        ],
    ))


@router.post("/data/refresh-quotes")
def refresh_quote_cache(
    request: Request, csrf_token: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_admin),
):
    enforce_csrf(request, csrf_token)
    tickers = [c.ticker for c in db.execute(
        select(Company).where(Company.status == "ACTIVE")).scalars().all()]
    stored = refresh_quotes(db, tickers) if tickers else {}
    audit(db, "quotes_refreshed", actor_email=viewer.user.email,
          ip=client_ip(request), count=len(stored))
    db.commit()
    return RedirectResponse("/admin/data", status_code=303)


@router.get("/emails", response_class=HTMLResponse)
def emails(
    request: Request, db: Session = Depends(get_db), viewer: Viewer = Depends(require_admin)
):
    rows = list(db.execute(
        select(EmailLog).order_by(desc(EmailLog.created_at)).limit(150)).scalars().all())
    return render(request, "gmg/admin/emails.html", _ctx(
        request, db, emails=rows, email=email_status()))


@router.get("/audit", response_class=HTMLResponse)
def audit_log(
    request: Request, q: str = Query(""), db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_admin),
):
    stmt = select(AuditLog).order_by(desc(AuditLog.created_at)).limit(300)
    if q:
        stmt = stmt.where(AuditLog.action.like(f"%{q.strip()}%"))
    return render(request, "gmg/admin/audit.html", _ctx(
        request, db, events=list(db.execute(stmt).scalars().all()), q=q))
