"""Subscription and billing routes.

What this module does not do is as important as what it does. It never marks a
payment as received on its own. A subscription becomes active only when a
payment provider that actually moved money says so, or when an administrator
confirms an out-of-band payment and leaves an audit record. There is no demo
gateway that flips payments to SUCCEEDED, because a database written by one
would be indistinguishable from a real one.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.accounts.service import audit
from backend.api.auth_deps import Viewer, client_ip, enforce_csrf, require_user
from backend.api.deps import gmg_context, render
from backend.api.routes_auth import flash_from
from backend.billing.payments import (
    CheckoutRequest,
    get_payment_provider,
    payment_status,
)
from backend.billing.subscriptions import (
    activate_subscription,
    cancel_subscription,
    current_plan,
    entitlement_state,
    get_subscription,
    resume_subscription,
    start_trial,
)
from backend.core.config import get_settings
from backend.core.database import get_db
from backend.core.logging_config import get_logger
from backend.data.saas_models import Payment, PaymentStatus, Subscription, User
from backend.notify.email_service import send_email

logger = get_logger(__name__)
router = APIRouter()


def _ctx(request: Request, db: Session, **extra: Any) -> dict[str, Any]:
    context = gmg_context(request, db, **extra)
    context.setdefault("flash", flash_from(request))
    return context


@router.get("/account/subscription", response_class=HTMLResponse)
def subscription_page(
    request: Request, required: str = "", db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_user),
):
    subscription = get_subscription(db, viewer.user)
    payments = list(db.execute(
        select(Payment).where(Payment.user_id == viewer.user.id)
        .order_by(desc(Payment.created_at)).limit(24)
    ).scalars().all())

    flash = flash_from(request)
    if required and not viewer.entitled:
        flash = {"kind": "info", "message":
                 "That page is part of the GMG subscription. Your access is checked on the "
                 "server, so it stays locked until a subscription is active."}

    return render(request, "gmg/subscription.html", _ctx(
        request, db, active="subscription", subscription=subscription,
        payments=payments, plan=current_plan().to_dict(),
        entitlement=entitlement_state(db, viewer.user),
        payments_status=payment_status(), flash=flash,
    ))


@router.post("/account/subscription/trial")
def begin_trial(
    request: Request, csrf_token: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    start_trial(db, viewer.user)
    audit(db, "trial_started", user=viewer.user, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/account/subscription?msg=trial_started", status_code=303)


@router.post("/account/subscription/checkout", response_class=HTMLResponse)
def checkout(
    request: Request, csrf_token: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    """Start a subscription purchase.

    With no gateway connected this records intent and says plainly that no card
    has been charged. It never activates the subscription itself.
    """
    enforce_csrf(request, csrf_token)
    settings = get_settings()
    plan = current_plan()
    provider = get_payment_provider()

    result = provider.create_checkout(db, CheckoutRequest(
        user=viewer.user, plan_code=plan.code, plan_name=plan.name,
        amount_egp=plan.price_egp, interval=plan.interval,
        return_url=f"{settings.base_url}/account/subscription",
    ))
    audit(db, "checkout_started", user=viewer.user, ip=client_ip(request),
          provider=provider.name, ok=result.ok)
    db.commit()

    if not result.ok:
        return render(request, "gmg/subscription.html", _ctx(
            request, db, active="subscription",
            subscription=get_subscription(db, viewer.user),
            payments=[], plan=plan.to_dict(),
            entitlement=entitlement_state(db, viewer.user),
            payments_status=payment_status(),
            flash={"kind": "error", "message": result.error or "Checkout could not be started."},
        ), status_code=400)

    if result.redirect_url:
        return RedirectResponse(result.redirect_url, status_code=303)

    return render(request, "gmg/checkout_pending.html", _ctx(
        request, db, active="subscription", message=result.message,
        payment=result.payment, plan=plan.to_dict(),
        payments_status=payment_status(),
    ))


@router.post("/account/subscription/cancel")
def cancel(
    request: Request, csrf_token: str = Form(""), reason: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    subscription = get_subscription(db, viewer.user)
    if subscription is not None:
        cancel_subscription(db, subscription, reason=(reason or "")[:255])
        audit(db, "subscription_cancelled", user=viewer.user, ip=client_ip(request))
        send_email(db, to=viewer.user.email, template="subscription_cancelled",
                   name=viewer.user.display_name,
                   ends_on=subscription.current_period_end)
        db.commit()
    return RedirectResponse("/account/subscription?msg=subscription_cancelled", status_code=303)


@router.post("/account/subscription/resume")
def resume(
    request: Request, csrf_token: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    subscription = get_subscription(db, viewer.user)
    if subscription is not None:
        resume_subscription(db, subscription)
        audit(db, "subscription_resumed", user=viewer.user, ip=client_ip(request))
        db.commit()
    return RedirectResponse("/account/subscription?msg=subscription_resumed", status_code=303)


@router.post("/api/payments/webhook")
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    """Gateway callback.

    The signature is verified by the provider implementation before anything is
    written. An unverified callback changes nothing — accepting one would let
    anyone grant themselves a subscription.
    """
    provider = get_payment_provider()
    payload = await request.body()
    result = provider.verify_webhook(payload, dict(request.headers))
    if not result.ok:
        logger.warning("Rejected payment webhook: %s", result.error)
        return JSONResponse({"detail": result.error or "Unverified callback."}, status_code=400)

    payment = db.scalar(
        select(Payment).where(Payment.external_id == result.payment_reference)
    )
    if payment is None:
        return JSONResponse({"detail": "Unknown payment reference."}, status_code=404)

    user = db.get(User, payment.user_id)
    if user is None:
        return JSONResponse({"detail": "Unknown payment reference."}, status_code=404)

    payment.status = result.status.value if result.status else PaymentStatus.PENDING.value
    if payment.status == PaymentStatus.SUCCEEDED.value:
        subscription = db.scalar(
            select(Subscription).where(Subscription.user_id == payment.user_id)
            .order_by(desc(Subscription.created_at))
        )
        if subscription is None:
            subscription = start_trial(db, user)
        activate_subscription(db, subscription, payment=payment)
        audit(db, "payment_confirmed", user=user, provider=provider.name,
              amount=payment.amount_egp)
        send_email(db, to=user.email, template="payment_succeeded",
                   name=user.display_name, amount=payment.amount_egp)
    elif payment.status == PaymentStatus.FAILED.value:
        send_email(db, to=user.email, template="payment_failed",
                   name=user.display_name, amount=payment.amount_egp)
    db.commit()
    return JSONResponse({"ok": True})
