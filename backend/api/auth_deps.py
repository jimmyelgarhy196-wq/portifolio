"""Session cookies, current-user resolution, and access control.

Access is decided here, on the server, on every request. The frontend never
decides what a user may see: a premium template is not rendered at all unless
:func:`backend.billing.subscriptions.user_is_entitled` returned True for this
request, so editing JavaScript, unhiding an element or calling the JSON API
directly cannot unlock anything.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend.accounts.security import (
    csrf_token_for,
    generate_token,
    sign_value,
    unsign_value,
    verify_csrf,
)
from backend.accounts.service import audit, resolve_session
from backend.billing.subscriptions import entitlement_state, user_is_entitled
from backend.core.config import get_settings
from backend.core.database import get_db
from backend.data.saas_models import User


class RedirectException(Exception):
    """Raised by a dependency to send the visitor somewhere else.

    Handled in :mod:`backend.api.app`; using an exception keeps the redirect
    decision inside the dependency that made it.
    """

    def __init__(self, url: str, *, status_code: int = 303) -> None:
        self.url = url
        self.status_code = status_code
        super().__init__(url)


class AccessDenied(Exception):
    """Signed in, but not permitted. Rendered as a 403 page."""

    def __init__(self, message: str = "You do not have access to this page.") -> None:
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------
def set_session_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.session_cookie,
        sign_value(raw_token),
        max_age=settings.session_days * 24 * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().session_cookie, path="/")


def read_session_token(request: Request) -> str | None:
    raw = request.cookies.get(get_settings().session_cookie)
    if not raw:
        return None
    return unsign_value(raw)


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------
@dataclass
class Viewer:
    """Who is asking, and what they are allowed to see."""

    user: User | None = None
    entitled: bool = False
    entitlement: dict[str, Any] | None = None
    csrf: str = ""

    @property
    def is_authenticated(self) -> bool:
        return self.user is not None

    @property
    def is_admin(self) -> bool:
        return bool(self.user and self.user.is_admin)


def get_viewer(request: Request, db: Session = Depends(get_db)) -> Viewer:
    """Resolve the signed-in user for this request. Never raises."""
    cached = getattr(request.state, "viewer", None)
    if cached is not None:
        return cached

    token = read_session_token(request)
    viewer = Viewer()
    if token:
        resolved = resolve_session(db, token)
        if resolved is not None:
            user, _session_row = resolved
            viewer.user = user
            viewer.csrf = csrf_token_for(token)
            viewer.entitled = user_is_entitled(db, user)
            viewer.entitlement = entitlement_state(db, user)
    if viewer.entitlement is None:
        viewer.entitlement = entitlement_state(db, None)
    request.state.viewer = viewer
    return viewer


def require_user(request: Request, viewer: Viewer = Depends(get_viewer)) -> Viewer:
    """Signed-in users only. Anonymous visitors go to the login page and are
    returned to where they were headed."""
    if not viewer.is_authenticated:
        nxt = request.url.path
        if request.url.query:
            nxt = f"{nxt}?{request.url.query}"
        raise RedirectException(f"/login?next={_quote(nxt)}")
    return viewer


def require_subscriber(request: Request, viewer: Viewer = Depends(require_user)) -> Viewer:
    """Paid features. The check happens here, server-side, before any premium
    template or JSON payload is produced."""
    if not viewer.entitled:
        nxt = request.url.path
        raise RedirectException(f"/account/subscription?required=1&from={_quote(nxt)}")
    return viewer


def require_admin(viewer: Viewer = Depends(require_user)) -> Viewer:
    if not viewer.is_admin:
        raise AccessDenied("This area is restricted to GMG administrators.")
    return viewer


def api_require_subscriber(viewer: Viewer = Depends(get_viewer)) -> Viewer:
    """Same rule for JSON endpoints, but answered with a status code rather
    than a redirect."""
    from fastapi import HTTPException

    if not viewer.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if not viewer.entitled:
        raise HTTPException(
            status_code=402,
            detail="An active GMG Investment Intelligence subscription is required.",
        )
    return viewer


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="/?=&")


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
#: Anonymous visitors get their own short-lived token so the sign-in and
#: sign-up forms are protected too. Without it, an attacker can silently sign a
#: victim into the attacker's account (login CSRF) and watch what they then
#: enter — which on this platform means portfolio holdings and watchlists.
FORM_COOKIE = "gmg_form"
FORM_TOKEN_TTL_SECONDS = 3600


def _form_secret(request: Request) -> str | None:
    raw = request.cookies.get(FORM_COOKIE)
    return unsign_value(raw) if raw else None


def form_token(request: Request) -> tuple[str, str | None]:
    """CSRF token for an anonymous form, plus the secret to persist if it is new.

    Returned as a pair so the token can be rendered into the page *before* the
    response exists, and the cookie set on that response afterwards.
    """
    secret = _form_secret(request)
    fresh = None
    if secret is None:
        secret = generate_token()
        fresh = secret
    return csrf_token_for(secret), fresh


def set_form_cookie(response: Response, secret: str) -> None:
    settings = get_settings()
    response.set_cookie(
        FORM_COOKIE, sign_value(secret),
        max_age=FORM_TOKEN_TTL_SECONDS, httponly=True,
        secure=settings.cookie_secure, samesite="lax", path="/",
    )


def check_csrf(request: Request, submitted: str | None) -> bool:
    """Every state-changing form posts a token derived from a secret we hold.

    Signed in, that secret is the session token. Signed out, it is a short-lived
    form cookie. Either way the token is *derived*, not stored server-side, so
    it needs no state and cannot be read cross-origin.
    """
    secret = read_session_token(request) or _form_secret(request)
    if secret is None:
        return False
    return bool(submitted) and verify_csrf(secret, submitted)


class CsrfError(Exception):
    pass


def enforce_csrf(request: Request, submitted: str | None) -> None:
    if not check_csrf(request, submitted):
        raise CsrfError("Your form session expired. Please try again.")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
_BUCKETS: dict[str, list[float]] = {}


def rate_limit(key: str, *, limit: int, window_seconds: int = 60) -> bool:
    """Fixed-window limiter, in process.

    Sufficient for one application node. A multi-node deployment should point
    this at Redis; the call sites do not change.
    """
    now = time.time()
    cutoff = now - window_seconds
    hits = [t for t in _BUCKETS.get(key, []) if t > cutoff]
    hits.append(now)
    _BUCKETS[key] = hits
    return len(hits) <= limit


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def reset_rate_limits() -> None:
    _BUCKETS.clear()


__all__ = [
    "AccessDenied", "CsrfError", "RedirectException", "Viewer",
    "FORM_COOKIE", "api_require_subscriber", "check_csrf", "clear_session_cookie",
    "client_ip", "enforce_csrf", "form_token", "get_viewer", "rate_limit",
    "read_session_token", "set_form_cookie",
    "require_admin", "require_subscriber", "require_user", "reset_rate_limits",
    "set_session_cookie",
]
