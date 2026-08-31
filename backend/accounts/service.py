"""Account operations: registration, login, sessions, verification, reset.

Every function here returns a result object rather than raising for expected
failures, so callers render a message instead of catching exceptions for
control flow.

Two behaviours are deliberate and worth not "fixing" later:

* **Login and password-reset responses do not reveal whether an account
  exists.** Both paths take a similar amount of work and return the same
  message either way, so the forms cannot be used to enumerate customers.
* **Changing a password bumps ``session_epoch``**, which invalidates every
  existing session immediately. That is what makes "reset my password" an
  effective response to a compromise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.accounts.security import (
    check_password_strength,
    generate_token,
    hash_password,
    hash_token,
    needs_rehash,
    normalise_email,
    validate_email_address,
    verify_password,
)
from backend.core.config import get_settings
from backend.core.logging_config import get_logger
from backend.data.saas_models import (
    AccountStatus,
    AuditLog,
    LoginAttempt,
    OneTimeToken,
    User,
    UserRole,
    UserSession,
)

logger = get_logger(__name__)

PURPOSE_VERIFY = "verify_email"
PURPOSE_RESET = "reset_password"

VERIFY_TTL_HOURS = 48
RESET_TTL_MINUTES = 60

#: Returned for both unknown-email and wrong-password so the two are
#: indistinguishable to anyone probing the login form.
GENERIC_LOGIN_ERROR = "Email or password is incorrect."


def now() -> datetime:
    return datetime.now(timezone.utc)


def _naive(dt: datetime) -> datetime:
    """SQLite columns are naive; store UTC without the tzinfo."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
def audit(
    session: Session, action: str, *, user: User | None = None,
    actor_email: str | None = None, target: str | None = None,
    ip: str | None = None, user_agent: str | None = None, **detail: Any,
) -> AuditLog:
    entry = AuditLog(
        user_id=user.id if user else None,
        actor_email=actor_email or (user.email if user else None),
        action=action, target=target, ip=ip,
        user_agent=(user_agent or "")[:255] or None,
        detail=detail or {},
    )
    session.add(entry)
    return entry


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class AuthResult:
    ok: bool
    user: User | None = None
    session_token: str | None = None
    error: str | None = None
    field_errors: dict[str, str] = field(default_factory=dict)
    #: Set when a verification or reset email should be sent by the caller.
    email_token: str | None = None
    email_purpose: str | None = None


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def get_user_by_email(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == normalise_email(email)))


def get_user(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def user_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(User)) or 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_user(
    session: Session, *, email: str, password: str, confirm_password: str | None = None,
    full_name: str | None = None, ip: str | None = None, user_agent: str | None = None,
    marketing_opt_in: bool = False,
) -> AuthResult:
    """Create an account and issue an email-verification token."""
    settings = get_settings()
    field_errors: dict[str, str] = {}

    valid, normalised = validate_email_address(email)
    if not valid:
        field_errors["email"] = "Enter a valid email address."
    else:
        email = normalised

    if confirm_password is not None and password != confirm_password:
        field_errors["confirm_password"] = "Passwords do not match."

    strength = check_password_strength(password or "", email=email)
    if not strength.ok:
        field_errors["password"] = f"Password {strength.message}."

    if field_errors:
        return AuthResult(ok=False, error="Please correct the errors below.",
                          field_errors=field_errors)

    if get_user_by_email(session, email) is not None:
        # An account exists. Do not confirm that to the caller — respond as if
        # the signup succeeded and let the email tell the real owner.
        audit(session, "signup_duplicate_email", actor_email=email, ip=ip)
        return AuthResult(
            ok=True, user=None,
            error=None,
            email_purpose="duplicate",
        )

    # The very first account becomes the admin, or the configured ADMIN_EMAIL.
    is_first = user_count(session) == 0
    is_configured_admin = (
        settings.admin_email and normalise_email(settings.admin_email) == email
    )
    role = UserRole.ADMIN.value if (is_first or is_configured_admin) else UserRole.USER.value

    user = User(
        email=email,
        password_hash=hash_password(password),
        full_name=(full_name or "").strip()[:160] or None,
        role=role,
        status=AccountStatus.ACTIVE.value,
        marketing_opt_in=bool(marketing_opt_in),
    )
    if not settings.require_email_verification:
        user.email_verified_at = _naive(now())
    session.add(user)
    session.flush()

    token = issue_token(session, user, PURPOSE_VERIFY, ttl=timedelta(hours=VERIFY_TTL_HOURS))
    audit(session, "signup", user=user, ip=ip, user_agent=user_agent, role=role)
    logger.info("Account created: %s (role=%s)", user.email, role)
    return AuthResult(ok=True, user=user, email_token=token, email_purpose=PURPOSE_VERIFY)


# ---------------------------------------------------------------------------
# One-time tokens
# ---------------------------------------------------------------------------
def issue_token(
    session: Session, user: User, purpose: str, *, ttl: timedelta,
    meta: dict[str, Any] | None = None, invalidate_existing: bool = True,
) -> str:
    """Create a single-use token and return the raw value (stored hashed)."""
    if invalidate_existing:
        for existing in session.execute(
            select(OneTimeToken).where(
                OneTimeToken.user_id == user.id,
                OneTimeToken.purpose == purpose,
                OneTimeToken.used_at.is_(None),
            )
        ).scalars().all():
            existing.used_at = _naive(now())

    raw = generate_token()
    session.add(OneTimeToken(
        user_id=user.id, purpose=purpose, token_hash=hash_token(raw),
        expires_at=_naive(now() + ttl), meta=meta or {},
    ))
    session.flush()
    return raw


def consume_token(session: Session, raw: str, purpose: str) -> User | None:
    """Validate and burn a token. Returns the owning user, or ``None``."""
    if not raw:
        return None
    record = session.scalar(
        select(OneTimeToken).where(
            OneTimeToken.token_hash == hash_token(raw),
            OneTimeToken.purpose == purpose,
        )
    )
    if record is None or not record.is_valid:
        return None
    record.used_at = _naive(now())
    return session.get(User, record.user_id)


def verify_email(session: Session, raw_token: str, *, ip: str | None = None) -> AuthResult:
    user = consume_token(session, raw_token, PURPOSE_VERIFY)
    if user is None:
        return AuthResult(ok=False, error="This verification link is invalid or has expired.")
    if user.email_verified_at is None:
        user.email_verified_at = _naive(now())
    audit(session, "email_verified", user=user, ip=ip)
    return AuthResult(ok=True, user=user)


def resend_verification(session: Session, email: str) -> AuthResult:
    user = get_user_by_email(session, email)
    if user is None or user.email_verified:
        # Same response either way: do not reveal registration status.
        return AuthResult(ok=True)
    token = issue_token(session, user, PURPOSE_VERIFY, ttl=timedelta(hours=VERIFY_TTL_HOURS))
    return AuthResult(ok=True, user=user, email_token=token, email_purpose=PURPOSE_VERIFY)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def _recent_failures(session: Session, email: str, ip: str | None) -> int:
    settings = get_settings()
    since = _naive(now() - timedelta(minutes=settings.login_window_minutes))
    stmt = select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.created_at >= since,
        LoginAttempt.successful.is_(False),
        LoginAttempt.email == normalise_email(email),
    )
    return session.scalar(stmt) or 0


def is_locked_out(session: Session, email: str, ip: str | None) -> bool:
    return _recent_failures(session, email, ip) >= get_settings().login_max_attempts


def authenticate(
    session: Session, *, email: str, password: str,
    ip: str | None = None, user_agent: str | None = None,
) -> AuthResult:
    """Verify credentials and open a session. Fails closed and uniformly."""
    settings = get_settings()
    email = normalise_email(email)

    if is_locked_out(session, email, ip):
        audit(session, "login_locked_out", actor_email=email, ip=ip)
        return AuthResult(
            ok=False,
            error=(
                f"Too many failed attempts. Try again in "
                f"{settings.login_lockout_minutes} minutes, or reset your password."
            ),
        )

    user = get_user_by_email(session, email)
    if user is None:
        # Spend comparable effort on an unknown email so response time does not
        # distinguish "no such account" from "wrong password".
        verify_password(password, "$argon2id$v=19$m=65536,t=3,p=4$" + "A" * 22 + "$" + "B" * 43)
        session.add(LoginAttempt(email=email, ip=ip, successful=False))
        audit(session, "login_failed", actor_email=email, ip=ip, reason="unknown_email")
        return AuthResult(ok=False, error=GENERIC_LOGIN_ERROR)

    if not verify_password(password, user.password_hash):
        session.add(LoginAttempt(email=email, ip=ip, successful=False))
        audit(session, "login_failed", user=user, ip=ip, reason="bad_password")
        return AuthResult(ok=False, error=GENERIC_LOGIN_ERROR)

    if user.status == AccountStatus.SUSPENDED.value:
        audit(session, "login_blocked_suspended", user=user, ip=ip)
        return AuthResult(
            ok=False,
            error="This account has been suspended. Contact support@gmg-ai.example.",
        )
    if user.status == AccountStatus.DELETED.value:
        return AuthResult(ok=False, error=GENERIC_LOGIN_ERROR)

    if settings.require_email_verification and not user.email_verified:
        token = issue_token(session, user, PURPOSE_VERIFY, ttl=timedelta(hours=VERIFY_TTL_HOURS))
        audit(session, "login_blocked_unverified", user=user, ip=ip)
        return AuthResult(
            ok=False, user=user,
            error="Please verify your email address. We have sent you a new link.",
            email_token=token, email_purpose=PURPOSE_VERIFY,
        )

    # Upgrade the stored hash if the cost parameters have since been raised.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    token = open_session(session, user, ip=ip, user_agent=user_agent)
    session.add(LoginAttempt(email=email, ip=ip, successful=True))
    user.last_login_at = _naive(now())
    user.last_login_ip = ip
    audit(session, "login", user=user, ip=ip, user_agent=user_agent)
    return AuthResult(ok=True, user=user, session_token=token)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def open_session(
    session: Session, user: User, *, ip: str | None = None, user_agent: str | None = None
) -> str:
    settings = get_settings()
    raw = generate_token()
    session.add(UserSession(
        user_id=user.id, token_hash=hash_token(raw), epoch=user.session_epoch,
        expires_at=_naive(now() + timedelta(days=settings.session_days)),
        ip=ip, user_agent=(user_agent or "")[:255] or None,
    ))
    session.flush()
    return raw


def resolve_session(session: Session, raw_token: str) -> tuple[User, UserSession] | None:
    """Return ``(user, session)`` for a valid token, else ``None``.

    Rejects the session if it is expired, revoked, the account is not active, or
    the user's epoch has moved past it (which is how a password change logs out
    every other device).
    """
    if not raw_token:
        return None
    record = session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    )
    if record is None or not record.is_valid:
        return None
    user = session.get(User, record.user_id)
    if user is None or user.status != AccountStatus.ACTIVE.value:
        return None
    if record.epoch != user.session_epoch:
        return None
    record.last_seen_at = _naive(now())
    return user, record


def close_session(session: Session, raw_token: str, *, ip: str | None = None) -> None:
    record = session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(raw_token))
    )
    if record is not None:
        record.revoked_at = _naive(now())
        audit(session, "logout", user=session.get(User, record.user_id), ip=ip)


def revoke_all_sessions(session: Session, user: User) -> int:
    """Invalidate every session by bumping the epoch. Returns sessions revoked."""
    user.session_epoch += 1
    live = session.execute(
        select(UserSession).where(
            UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
        )
    ).scalars().all()
    for record in live:
        record.revoked_at = _naive(now())
    return len(live)


# ---------------------------------------------------------------------------
# Password reset and change
# ---------------------------------------------------------------------------
def request_password_reset(
    session: Session, email: str, *, ip: str | None = None
) -> AuthResult:
    """Always reports success so the form cannot enumerate accounts."""
    user = get_user_by_email(session, email)
    if user is None or user.status == AccountStatus.DELETED.value:
        audit(session, "password_reset_requested_unknown", actor_email=normalise_email(email), ip=ip)
        return AuthResult(ok=True)
    token = issue_token(session, user, PURPOSE_RESET, ttl=timedelta(minutes=RESET_TTL_MINUTES))
    audit(session, "password_reset_requested", user=user, ip=ip)
    return AuthResult(ok=True, user=user, email_token=token, email_purpose=PURPOSE_RESET)


def reset_password(
    session: Session, *, raw_token: str, password: str, confirm_password: str | None = None,
    ip: str | None = None,
) -> AuthResult:
    user = consume_token(session, raw_token, PURPOSE_RESET)
    if user is None:
        return AuthResult(ok=False, error="This reset link is invalid or has expired.")

    if confirm_password is not None and password != confirm_password:
        return AuthResult(ok=False, error="Passwords do not match.",
                          field_errors={"confirm_password": "Passwords do not match."})
    strength = check_password_strength(password, email=user.email)
    if not strength.ok:
        return AuthResult(ok=False, error=f"Password {strength.message}.",
                          field_errors={"password": f"Password {strength.message}."})

    user.password_hash = hash_password(password)
    # A reset is the remedy for a compromise, so it must end every session.
    revoked = revoke_all_sessions(session, user)
    if user.email_verified_at is None:
        # Completing a reset proves control of the mailbox.
        user.email_verified_at = _naive(now())
    audit(session, "password_reset", user=user, ip=ip, sessions_revoked=revoked)
    return AuthResult(ok=True, user=user)


def change_password(
    session: Session, user: User, *, current_password: str, new_password: str,
    confirm_password: str | None = None, ip: str | None = None,
) -> AuthResult:
    if not verify_password(current_password, user.password_hash):
        audit(session, "password_change_failed", user=user, ip=ip)
        return AuthResult(ok=False, error="Your current password is incorrect.")
    if confirm_password is not None and new_password != confirm_password:
        return AuthResult(ok=False, error="New passwords do not match.")
    strength = check_password_strength(new_password, email=user.email)
    if not strength.ok:
        return AuthResult(ok=False, error=f"Password {strength.message}.")

    user.password_hash = hash_password(new_password)
    revoke_all_sessions(session, user)
    token = open_session(session, user, ip=ip)   # keep the caller signed in
    audit(session, "password_changed", user=user, ip=ip)
    return AuthResult(ok=True, user=user, session_token=token)


# ---------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------
def set_account_status(
    session: Session, target: User, status: AccountStatus, *,
    actor: User | None = None, ip: str | None = None,
) -> None:
    target.status = status.value
    if status is not AccountStatus.ACTIVE:
        revoke_all_sessions(session, target)
    audit(
        session, f"account_{status.value.lower()}", user=actor,
        actor_email=actor.email if actor else None,
        target=target.email, ip=ip, target_user_id=target.id,
    )


def set_role(
    session: Session, target: User, role: UserRole, *,
    actor: User | None = None, ip: str | None = None,
) -> None:
    target.role = role.value
    audit(session, "role_changed", user=actor,
          actor_email=actor.email if actor else None,
          target=target.email, ip=ip, new_role=role.value)
