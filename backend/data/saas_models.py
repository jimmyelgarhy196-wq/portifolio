"""SaaS domain models: accounts, billing, user-owned data and audit.

These extend the research schema in ``models.py`` rather than replacing it.
Design rules carried through every table here:

* **Ownership is explicit.** Every user-owned row carries ``user_id`` with a
  cascading delete, and every query path filters on it. There is no table where
  a missing filter would leak one user's data to another.
* **Secrets are never stored in the clear.** Passwords are Argon2 hashes;
  session and one-time tokens are stored as SHA-256 digests, so a database
  disclosure does not hand over live sessions.
* **Money is recorded, never simulated.** ``payments`` records intent and
  externally-confirmed state. Nothing in this codebase charges a card.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.data.models import Base, utcnow


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class SubscriptionStatus(str, Enum):
    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELLED = "CANCELLED"     # cancelled but still inside the paid period
    EXPIRED = "EXPIRED"

    @property
    def grants_access(self) -> bool:
        """Whether this status alone entitles the user to premium features.

        CANCELLED still grants access until ``current_period_end`` passes; that
        date check lives in :meth:`Subscription.is_entitled`.
        """
        return self in (SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE,
                        SubscriptionStatus.CANCELLED)


class PaymentStatus(str, Enum):
    PENDING = "PENDING"         # intent recorded, awaiting the gateway
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"


class UserRole(str, Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELETED = "DELETED"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_email_lower", "email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Stored lower-cased; uniqueness is therefore case-insensitive.
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    #: Argon2id hash. The plaintext password never leaves the request handler.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.USER.value, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=AccountStatus.ACTIVE.value, nullable=False)

    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(64))
    #: Bumped on password change so every existing session is invalidated.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
        order_by="Subscription.created_at.desc()",
    )
    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value

    @property
    def is_active(self) -> bool:
        return self.status == AccountStatus.ACTIVE.value

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def display_name(self) -> str:
        return self.full_name or self.email.split("@")[0]

    def to_dict(self, *, admin_view: bool = False) -> dict[str, Any]:
        payload = {
            "id": self.id, "email": self.email, "full_name": self.full_name,
            "role": self.role, "status": self.status,
            "email_verified": self.email_verified,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if admin_view:
            payload.update({
                "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
                "last_login_ip": self.last_login_ip,
                "marketing_opt_in": self.marketing_opt_in,
            })
        return payload


class UserSession(Base):
    """A logged-in session. The cookie carries the id; the secret is hashed here."""

    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_sessions_user_expiry", "user_id", "expires_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: SHA-256 of the session secret. A database leak yields no usable session.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    #: Session is rejected if the user's epoch has moved past this value.
    epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped["User"] = relationship(back_populates="sessions")

    @property
    def is_valid(self) -> bool:
        if self.revoked_at is not None:
            return False
        expiry = self.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > datetime.now(timezone.utc)


class OneTimeToken(Base):
    """Email verification and password reset tokens. Single use, time limited."""

    __tablename__ = "one_time_tokens"
    __table_args__ = (Index("ix_ott_user_purpose", "user_id", "purpose"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: verify_email | reset_password | change_email
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    @property
    def is_valid(self) -> bool:
        if self.used_at is not None:
            return False
        expiry = self.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > datetime.now(timezone.utc)


class LoginAttempt(Base):
    """Failed-login record backing rate limiting and lockout."""

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_email_time", "email", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), index=True)
    successful: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subs_user_status", "user_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_code: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=SubscriptionStatus.TRIAL.value, nullable=False, index=True
    )
    price_egp: Mapped[float] = mapped_column(Float, nullable=False)
    interval: Mapped[str] = mapped_column(String(16), default="month")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)

    #: Identifier at the payment gateway, once one is connected.
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    provider: Mapped[str | None] = mapped_column(String(48))
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan",
        order_by="Payment.created_at.desc()",
    )

    def is_entitled(self, at: datetime | None = None) -> bool:
        """Whether this subscription grants premium access right now.

        Status is necessary but not sufficient: a CANCELLED subscription keeps
        access until the period it was paid for actually ends.
        """
        try:
            status = SubscriptionStatus(self.status)
        except ValueError:
            return False
        if not status.grants_access:
            return False

        now = at or datetime.now(timezone.utc)
        deadline = (
            self.trial_ends_at if status is SubscriptionStatus.TRIAL
            else self.current_period_end
        )
        if deadline is None:
            # No period recorded: only an explicitly ACTIVE subscription counts.
            return status is SubscriptionStatus.ACTIVE
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline > now

    @property
    def days_remaining(self) -> int | None:
        deadline = (
            self.trial_ends_at if self.status == SubscriptionStatus.TRIAL.value
            else self.current_period_end
        )
        if deadline is None:
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0, (deadline - datetime.now(timezone.utc)).days)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "user_id": self.user_id,
            "plan_code": self.plan_code, "plan_name": self.plan_name,
            "status": self.status, "price_egp": self.price_egp,
            "interval": self.interval,
            "entitled": self.is_entitled(),
            "days_remaining": self.days_remaining,
            "trial_ends_at": self.trial_ends_at.isoformat() if self.trial_ends_at else None,
            "current_period_end": (
                self.current_period_end.isoformat() if self.current_period_end else None
            ),
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "provider": self.provider, "external_id": self.external_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Payment(Base):
    """A payment record.

    Nothing in this codebase charges a card. Rows are created as PENDING intent
    and only move to SUCCEEDED when a real gateway (or an admin, in manual mode)
    confirms it.
    """

    __tablename__ = "payments"
    __table_args__ = (Index("ix_payments_user_time", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), index=True
    )
    amount_egp: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="EGP")
    status: Mapped[str] = mapped_column(
        String(16), default=PaymentStatus.PENDING.value, nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(48), default="manual")
    #: Gateway reference, once a gateway exists.
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(String(255))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    subscription: Mapped["Subscription | None"] = relationship(back_populates="payments")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "amount_egp": self.amount_egp, "currency": self.currency,
            "status": self.status, "provider": self.provider,
            "external_id": self.external_id, "description": self.description,
            "failure_reason": self.failure_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "settled_at": self.settled_at.isoformat() if self.settled_at else None,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }


# ---------------------------------------------------------------------------
# User-owned research data
# ---------------------------------------------------------------------------
class UserWatchlist(Base):
    __tablename__ = "user_watchlists"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_watchlist_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    items: Mapped[list["UserWatchlistItem"]] = relationship(
        back_populates="watchlist", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "count": len(self.items),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserWatchlistItem(Base):
    __tablename__ = "user_watchlist_items"
    __table_args__ = (
        UniqueConstraint("watchlist_id", "ticker", name="uq_watchlist_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("user_watchlists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text)
    target_price: Mapped[float | None] = mapped_column(Float)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    watchlist: Mapped["UserWatchlist"] = relationship(back_populates="items")


class UserPortfolio(Base):
    """A tracked portfolio. Tracking only — this platform executes no trades."""

    __tablename__ = "user_portfolios"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_portfolio_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="EGP")
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    positions: Mapped[list["UserPosition"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "currency": self.currency,
            "description": self.description, "positions": len(self.positions),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserPosition(Base):
    __tablename__ = "user_portfolio_positions"
    __table_args__ = (Index("ix_userpos_portfolio", "portfolio_id", "ticker"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("user_portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    shares: Mapped[float] = mapped_column(Float, nullable=False)
    purchase_price: Mapped[float] = mapped_column(Float, nullable=False)
    purchase_date: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    portfolio: Mapped["UserPortfolio"] = relationship(back_populates="positions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "ticker": self.ticker, "shares": self.shares,
            "purchase_price": self.purchase_price,
            "purchase_date": self.purchase_date.isoformat() if self.purchase_date else None,
            "note": self.note,
        }


class SavedScreen(Base):
    __tablename__ = "saved_screens"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_screen_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "filters": self.filters or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserAlert(Base):
    """A price or indicator alert owned by a user."""

    __tablename__ = "user_alerts"
    __table_args__ = (Index("ix_useralert_active", "user_id", "active"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    #: price_above | price_below | pct_move | rsi_above | rsi_below
    #: | high_52w | low_52w | ma_cross
    condition: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_delivery: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime)
    trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message: Mapped[str | None] = mapped_column(Text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "ticker": self.ticker, "condition": self.condition,
            "threshold": self.threshold, "note": self.note, "active": self.active,
            "email_delivery": self.email_delivery,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_triggered_at": (
                self.last_triggered_at.isoformat() if self.last_triggered_at else None
            ),
            "trigger_count": self.trigger_count,
            "last_message": self.last_message,
        }


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
class AuditLog(Base):
    """Security-relevant events. Append-only; never updated or deleted in code."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_action_time", "action", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Nullable so failed logins for unknown emails are still recorded.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(160))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "user_id": self.user_id, "actor_email": self.actor_email,
            "action": self.action, "target": self.target, "ip": self.ip,
            "detail": self.detail or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class EmailLog(Base):
    """Every email the system attempted, with its outcome."""

    __tablename__ = "email_log"
    __table_args__ = (Index("ix_email_to_time", "to_email", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    to_email: Mapped[str] = mapped_column(String(255), nullable=False)
    template: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="console")
    status: Mapped[str] = mapped_column(String(16), default="SENT")   # SENT|FAILED|SKIPPED
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "to_email": self.to_email, "template": self.template,
            "subject": self.subject, "provider": self.provider, "status": self.status,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Quote(Base):
    """Latest market snapshot per instrument.

    One row per ticker, updated in place: this is a cache of the current
    snapshot, not a history (that is ``price_history``). Provenance columns are
    explicit rather than inherited so ``delayed_minutes`` and ``market_status``
    sit alongside them.
    """

    __tablename__ = "market_quotes"

    ticker: Mapped[str] = mapped_column(String(24), primary_key=True)
    price: Mapped[float | None] = mapped_column(Float)
    previous_close: Mapped[float | None] = mapped_column(Float)
    change: Mapped[float | None] = mapped_column(Float)
    change_pct: Mapped[float | None] = mapped_column(Float)
    open: Mapped[float | None] = mapped_column(Float)
    day_high: Mapped[float | None] = mapped_column(Float)
    day_low: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    turnover: Mapped[float | None] = mapped_column(Float)
    trades: Mapped[int | None] = mapped_column(Integer)
    week52_high: Mapped[float | None] = mapped_column(Float)
    week52_low: Mapped[float | None] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="EGP")

    quote_time: Mapped[datetime | None] = mapped_column(DateTime)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source: Mapped[str] = mapped_column(String(64), default="UNKNOWN")
    #: 0 = real time. Surfaced in the UI so freshness is never hidden.
    delayed_minutes: Mapped[int] = mapped_column(Integer, default=0)
    market_status: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker, "price": self.price,
            "previous_close": self.previous_close,
            "change": self.change, "change_pct": self.change_pct,
            "open": self.open, "day_high": self.day_high, "day_low": self.day_low,
            "volume": self.volume, "turnover": self.turnover, "trades": self.trades,
            "week52_high": self.week52_high, "week52_low": self.week52_low,
            "market_cap": self.market_cap, "currency": self.currency,
            "quote_time": self.quote_time.isoformat() if self.quote_time else None,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "source": self.source, "delayed_minutes": self.delayed_minutes,
            "market_status": self.market_status, "is_demo": self.is_demo,
        }


class DataSourceRecord(Base):
    """Registry of configured data sources and their last known health."""

    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)   # quotes|prices|...
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_credentials: Mapped[bool] = mapped_column(Boolean, default=False)
    credentials_present: Mapped[bool] = mapped_column(Boolean, default=False)
    delayed_minutes: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "enabled": self.enabled,
            "is_demo": self.is_demo,
            "requires_credentials": self.requires_credentials,
            "credentials_present": self.credentials_present,
            "delayed_minutes": self.delayed_minutes,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "last_error": self.last_error, "notes": self.notes,
        }
