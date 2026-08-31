"""Account routes: signup, sign-in, verification, password reset, profile.

Design notes that matter more than the routing:

* Responses never reveal whether an email address has an account. Sign-up with
  an existing address and a password-reset request both return the same
  confirmation the real owner would see; only the email that lands differs.
* Sign-in failures are a single generic message, and lockout is applied per
  email and per IP address.
* Every state-changing form carries a CSRF token derived from the session.
* Passwords are never written to a log, an audit row, or an error message.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.accounts import service as accounts
from backend.accounts.security import check_password_strength
from backend.api.auth_deps import (
    CsrfError,
    RedirectException,
    Viewer,
    clear_session_cookie,
    client_ip,
    enforce_csrf,
    form_token,
    get_viewer,
    set_form_cookie,
    rate_limit,
    read_session_token,
    require_user,
    set_session_cookie,
)
from backend.api.deps import gmg_context, render
from backend.billing.subscriptions import (
    current_plan,
    entitlement_state,
    get_subscription,
    start_trial,
)
from backend.core.config import get_settings
from backend.core.database import get_db
from backend.core.logging_config import get_logger
from backend.data.saas_models import AuditLog, EmailLog, UserSession
from backend.notify.email_service import send_email

logger = get_logger(__name__)
router = APIRouter()

#: Flash messages are looked up by code so no user-supplied text can be
#: reflected into a page through the query string.
FLASH: dict[str, tuple[str, str]] = {
    "verify_sent": ("info", "Check your inbox — we have sent you a verification link."),
    "verified": ("ok", "Your email address is verified. Welcome to GMG."),
    "verify_failed": ("error", "That verification link is invalid or has expired. Request a new one below."),
    "reset_sent": ("info", "If that email address has a GMG account, a password-reset link is on its way."),
    "reset_done": ("ok", "Your password has been changed. Please sign in."),
    "reset_failed": ("error", "That password-reset link is invalid or has expired. Request a new one."),
    "signed_out": ("ok", "You have been signed out."),
    "password_changed": ("ok", "Your password has been updated. Other devices have been signed out."),
    "profile_saved": ("ok", "Your profile has been updated."),
    "trial_started": ("ok", "Your free trial is active. Full access is open until it ends."),
    "subscription_cancelled": ("info", "Your subscription will not renew. Access continues until the end of the paid period."),
    "subscription_resumed": ("ok", "Your subscription will renew as normal."),
    "checkout_pending": ("info", "Your subscription request has been recorded. No card has been charged."),
    "login_required": ("info", "Please sign in to continue."),
    "sessions_revoked": ("ok", "All other sessions have been signed out."),
    "watchlist_created": ("ok", "Watchlist created."),
    "watchlist_exists": ("error", "You already have a watchlist with that name."),
    "watchlist_name_required": ("error", "Give the watchlist a name."),
    "invalid_position": ("error",
                         "Enter a positive number of shares and a positive purchase price."),
    "alert_needs_threshold": ("error", "That alert condition needs a threshold value."),
    "payment_confirmed": ("ok", "Payment confirmed and the subscription activated."),
    "already_confirmed": ("info", "That payment was already confirmed."),
    "self_change_blocked": ("error",
                            "You cannot change your own role or account status."),
}


def flash_from(request: Request) -> dict[str, str] | None:
    code = request.query_params.get("msg")
    if code in FLASH:
        kind, message = FLASH[code]
        return {"kind": kind, "message": message}
    return None


def _ctx(request: Request, db: Session, **extra: Any) -> dict[str, Any]:
    context = gmg_context(request, db, **extra)
    context.setdefault("flash", flash_from(request))
    return context


def render_form(request: Request, db: Session, template: str, *,
                status_code: int = 200, **extra: Any):
    """Render an anonymous form, issuing the CSRF cookie it will post back.

    Sign-in and sign-up are protected too: without this an attacker could sign a
    victim into their own account and observe what the victim enters next —
    which on this platform means portfolio holdings and watchlists.
    """
    token, fresh_secret = form_token(request)
    context = _ctx(request, db, **extra)
    context["csrf_token"] = token
    response = render(request, template, context, status_code=status_code)
    if fresh_secret is not None:
        set_form_cookie(response, fresh_secret)
    return response


def _safe_next(value: str | None, fallback: str = "/market") -> str:
    """Only same-site paths. An absolute URL in ?next= is an open redirect."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return fallback
    return value


# ---------------------------------------------------------------------------
# Sign up
# ---------------------------------------------------------------------------
@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, db: Session = Depends(get_db)):
    viewer = get_viewer(request, db)
    if viewer.is_authenticated:
        return RedirectResponse("/market", status_code=303)
    return render_form(request, db, "gmg/signup.html",
                       plan=current_plan().to_dict(), form={}, errors={})


@router.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    csrf_token: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    full_name: str = Form(""),
    accept_terms: str = Form(""),
    marketing_opt_in: str = Form(""),
    db: Session = Depends(get_db),
):
    enforce_csrf(request, csrf_token)
    ip = client_ip(request)
    settings = get_settings()
    form = {"email": email, "full_name": full_name,
            "marketing_opt_in": bool(marketing_opt_in)}

    if not rate_limit(f"signup:{ip}", limit=6, window_seconds=600):
        return render_form(
            request, db, "gmg/signup.html", status_code=429,
            plan=current_plan().to_dict(), form=form, errors={},
            flash={"kind": "error", "message":
                   "Too many sign-up attempts from this network. Try again shortly."})

    if not accept_terms:
        return render_form(
            request, db, "gmg/signup.html", status_code=400,
            plan=current_plan().to_dict(), form=form,
            errors={"accept_terms": "You must accept the terms and the risk disclaimer."},
            flash={"kind": "error", "message": "Please correct the errors below."})

    result = accounts.register_user(
        db, email=email, password=password, confirm_password=confirm_password,
        full_name=full_name, ip=ip, user_agent=request.headers.get("user-agent"),
        marketing_opt_in=bool(marketing_opt_in),
    )
    if not result.ok:
        return render_form(
            request, db, "gmg/signup.html", status_code=400,
            plan=current_plan().to_dict(), form=form, errors=result.field_errors,
            flash={"kind": "error", "message": result.error or "Please check the form."})

    if result.user is not None:
        start_trial(db, result.user)
        db.commit()
        link = f"{settings.base_url}/verify?token={result.email_token}"
        send_email(db, to=result.user.email, template="verify_email", user=result.user,
                   name=result.user.display_name, link=link,
                   trial_days=settings.trial_days)
        send_email(db, to=result.user.email, template="welcome", user=result.user,
                   name=result.user.display_name, trial_days=settings.trial_days)
        db.commit()
    else:
        # Address already registered. Same response, different email.
        db.commit()

    return RedirectResponse("/signup/check-email", status_code=303)


@router.get("/signup/check-email", response_class=HTMLResponse)
def check_email(request: Request, db: Session = Depends(get_db)):
    return render(request, "gmg/check_email.html", _ctx(request, db))


@router.get("/verify", response_class=HTMLResponse)
def verify(request: Request, token: str = "", db: Session = Depends(get_db)):
    result = accounts.verify_email(db, token, ip=client_ip(request))
    db.commit()
    return RedirectResponse(
        "/login?msg=" + ("verified" if result.ok else "verify_failed"), status_code=303
    )


@router.post("/verify/resend")
def resend_verification(
    request: Request, csrf_token: str = Form(""), email: str = Form(""),
    db: Session = Depends(get_db),
):
    enforce_csrf(request, csrf_token)
    ip = client_ip(request)
    if not rate_limit(f"resend:{ip}", limit=5, window_seconds=900):
        return RedirectResponse("/login?msg=verify_sent", status_code=303)
    result = accounts.resend_verification(db, email)
    if result.ok and result.email_token and result.user:
        link = f"{get_settings().base_url}/verify?token={result.email_token}"
        send_email(db, to=result.user.email, template="verify_email",
                   name=result.user.display_name, link=link,
                   trial_days=get_settings().trial_days)
    db.commit()
    return RedirectResponse("/login?msg=verify_sent", status_code=303)


# ---------------------------------------------------------------------------
# Sign in / out
# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "", db: Session = Depends(get_db)):
    viewer = get_viewer(request, db)
    if viewer.is_authenticated:
        return RedirectResponse(_safe_next(next), status_code=303)
    return render_form(request, db, "gmg/login.html",
                       next=_safe_next(next), form={})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    csrf_token: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    enforce_csrf(request, csrf_token)
    ip = client_ip(request)
    if not rate_limit(f"login:{ip}", limit=12, window_seconds=300):
        return render_form(
            request, db, "gmg/login.html", status_code=429,
            next=_safe_next(next), form={"email": email},
            flash={"kind": "error", "message":
                   "Too many sign-in attempts from this network. Wait a few minutes."})

    result = accounts.authenticate(
        db, email=email, password=password, ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    if not result.ok:
        # Blocked because the address is unverified: authenticate() has already
        # issued a fresh link, so send it.
        if result.email_purpose == accounts.PURPOSE_VERIFY and result.user is not None:
            link = f"{get_settings().base_url}/verify?token={result.email_token}"
            send_email(db, to=result.user.email, template="verify_email",
                       name=result.user.display_name, link=link,
                       trial_days=get_settings().trial_days)
        db.commit()
        return render_form(
            request, db, "gmg/login.html", status_code=401,
            next=_safe_next(next), form={"email": email},
            flash={"kind": "error", "message": result.error or accounts.GENERIC_LOGIN_ERROR},
            unverified=result.email_purpose == accounts.PURPOSE_VERIFY)

    # authenticate() opened the session; do not open a second one.
    db.commit()
    response = RedirectResponse(_safe_next(next), status_code=303)
    set_session_cookie(response, result.session_token)
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    try:
        enforce_csrf(request, csrf_token)
    except CsrfError:
        return RedirectResponse("/market", status_code=303)
    raw = read_session_token(request)
    if raw:
        accounts.close_session(db, raw, ip=client_ip(request))
        db.commit()
    response = RedirectResponse("/login?msg=signed_out", status_code=303)
    clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------
@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_form(request: Request, db: Session = Depends(get_db)):
    return render_form(request, db, "gmg/forgot_password.html")


@router.post("/forgot-password")
def forgot_submit(request: Request, csrf_token: str = Form(""),
                  email: str = Form(""), db: Session = Depends(get_db)):
    enforce_csrf(request, csrf_token)
    ip = client_ip(request)
    if rate_limit(f"forgot:{ip}", limit=6, window_seconds=900):
        result = accounts.request_password_reset(db, email=email, ip=ip)
        if result.email_token and result.user is not None:
            link = f"{get_settings().base_url}/reset-password?token={result.email_token}"
            send_email(db, to=result.user.email, template="reset_password",
                       name=result.user.display_name, link=link,
                       minutes=accounts.RESET_TTL_MINUTES)
        db.commit()
    # Identical response either way: this must not confirm whether the account exists.
    return RedirectResponse("/login?msg=reset_sent", status_code=303)


@router.get("/reset-password", response_class=HTMLResponse)
def reset_form(request: Request, token: str = "", db: Session = Depends(get_db)):
    return render_form(request, db, "gmg/reset_password.html", token=token)


@router.post("/reset-password", response_class=HTMLResponse)
def reset_submit(
    request: Request,
    csrf_token: str = Form(""),
    token: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    enforce_csrf(request, csrf_token)
    if not rate_limit(f"reset:{client_ip(request)}", limit=8, window_seconds=900):
        return RedirectResponse("/login?msg=reset_failed", status_code=303)
    result = accounts.reset_password(
        db, raw_token=token, password=password, confirm_password=confirm_password,
        ip=client_ip(request),
    )
    db.commit()
    if not result.ok:
        if result.field_errors:
            return render_form(request, db, "gmg/reset_password.html",
                   token=token, errors=result.field_errors,
                flash={"kind": "error", "message": result.error or "Please check the form."},
               status_code=400)
        return RedirectResponse("/login?msg=reset_failed", status_code=303)
    return RedirectResponse("/login?msg=reset_done", status_code=303)


# ---------------------------------------------------------------------------
# Account area
# ---------------------------------------------------------------------------
@router.get("/account", response_class=HTMLResponse)
def account(request: Request, viewer: Viewer = Depends(require_user),
            db: Session = Depends(get_db)):
    sessions = list(db.execute(
        select(UserSession).where(
            UserSession.user_id == viewer.user.id, UserSession.revoked_at.is_(None)
        ).order_by(UserSession.last_seen_at.desc()).limit(12)
    ).scalars().all())
    recent = list(db.execute(
        select(AuditLog).where(AuditLog.user_id == viewer.user.id)
        .order_by(AuditLog.created_at.desc()).limit(15)
    ).scalars().all())
    return render(request, "gmg/account.html", _ctx(
        request, db, active="account", sessions=sessions, audit=recent,
        entitlement=entitlement_state(db, viewer.user),
        subscription=get_subscription(db, viewer.user),
    ))


@router.post("/account/profile")
def update_profile(
    request: Request,
    csrf_token: str = Form(""),
    full_name: str = Form(""),
    marketing_opt_in: str = Form(""),
    viewer: Viewer = Depends(require_user),
    db: Session = Depends(get_db),
):
    enforce_csrf(request, csrf_token)
    user = viewer.user
    user.full_name = (full_name or "").strip()[:160] or None
    user.marketing_opt_in = bool(marketing_opt_in)
    accounts.audit(db, "profile_updated", user=user, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/account?msg=profile_saved", status_code=303)


@router.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    csrf_token: str = Form(""),
    current_password: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    viewer: Viewer = Depends(require_user),
    db: Session = Depends(get_db),
):
    enforce_csrf(request, csrf_token)
    result = accounts.change_password(
        db, user=viewer.user, current_password=current_password,
        password=password, confirm_password=confirm_password, ip=client_ip(request),
    )
    db.commit()
    if not result.ok:
        return render(request, "gmg/account.html", _ctx(
            request, db, active="account", sessions=[], audit=[],
            entitlement=entitlement_state(db, viewer.user),
            subscription=get_subscription(db, viewer.user),
            errors=result.field_errors,
            flash={"kind": "error", "message": result.error or "Password not changed."},
        ), status_code=400)
    # Every session, including this one, was revoked. Sign in again.
    response = RedirectResponse("/login?msg=password_changed", status_code=303)
    clear_session_cookie(response)
    return response


@router.post("/account/sessions/revoke")
def revoke_sessions(
    request: Request, csrf_token: str = Form(""),
    viewer: Viewer = Depends(require_user), db: Session = Depends(get_db),
):
    enforce_csrf(request, csrf_token)
    accounts.revoke_all_sessions(db, viewer.user)
    db.commit()
    response = RedirectResponse("/login?msg=sessions_revoked", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/account/data", response_class=HTMLResponse)
def my_data(request: Request, viewer: Viewer = Depends(require_user),
            db: Session = Depends(get_db)):
    """What GMG holds about this account, and how to have it deleted."""
    emails = list(db.execute(
        select(EmailLog).where(EmailLog.to_email == viewer.user.email)
        .order_by(EmailLog.created_at.desc()).limit(25)
    ).scalars().all())
    audit = list(db.execute(
        select(AuditLog).where(AuditLog.user_id == viewer.user.id)
        .order_by(AuditLog.created_at.desc()).limit(50)
    ).scalars().all())
    return render(request, "gmg/my_data.html", _ctx(
        request, db, active="account", emails=emails, audit=audit,
    ))
