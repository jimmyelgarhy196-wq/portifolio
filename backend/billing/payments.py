"""Payment provider abstraction.

**No payment processing happens in this codebase.** There is no card handling,
no simulated success, and no path that marks money as received on its own. What
exists is the interface an Egyptian gateway (Paymob, Fawry, Kashier, Paytabs)
plugs into, plus a manual provider that records intent and waits for a human or
a webhook to confirm.

That distinction matters: a "demo gateway" that flips payments to SUCCEEDED
would be indistinguishable from a real one in the database, and the first time
somebody trusted the revenue figures they would be wrong.

Connecting a real gateway means implementing :class:`PaymentProvider` — a
checkout hand-off and a webhook verifier — and registering it below.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.core.logging_config import get_logger
from backend.data.saas_models import Payment, PaymentStatus, Subscription, User

logger = get_logger(__name__)


@dataclass
class CheckoutRequest:
    user: User
    plan_code: str
    plan_name: str
    amount_egp: float
    interval: str = "month"
    return_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckoutResult:
    ok: bool
    #: Where to send the customer. ``None`` when no gateway is connected.
    redirect_url: str | None = None
    payment: Payment | None = None
    #: True when the customer must complete payment somewhere else.
    requires_external_action: bool = False
    message: str = ""
    error: str | None = None


@dataclass
class WebhookResult:
    ok: bool
    payment_reference: str | None = None
    status: PaymentStatus | None = None
    error: str | None = None


class PaymentProvider(abc.ABC):
    name = "base"
    display_name = "Base"

    @abc.abstractmethod
    def create_checkout(self, session: Session, request: CheckoutRequest) -> CheckoutResult:
        ...

    def verify_webhook(self, payload: bytes, headers: dict[str, str]) -> WebhookResult:
        """Verify a gateway callback. Must authenticate the signature."""
        return WebhookResult(ok=False, error=f"{self.name} does not implement webhooks.")

    def is_configured(self) -> bool:
        return True

    def status_note(self) -> str:
        return ""

    @property
    def processes_payments(self) -> bool:
        """Whether this provider actually moves money."""
        return False


class ManualPaymentProvider(PaymentProvider):
    """Records payment intent. Confirmation is a deliberate human action.

    This is the honest default: a subscription created here sits in PENDING
    until an administrator confirms that money arrived out of band (bank
    transfer, Instapay, Vodafone Cash). Nothing is ever auto-approved.
    """

    name = "manual"
    display_name = "Manual / bank transfer"

    def create_checkout(self, session: Session, request: CheckoutRequest) -> CheckoutResult:
        payment = Payment(
            user_id=request.user.id,
            amount_egp=request.amount_egp,
            currency="EGP",
            status=PaymentStatus.PENDING.value,
            provider=self.name,
            description=f"{request.plan_name} — {request.interval}ly subscription",
            period_start=date.today(),
            period_end=date.today() + timedelta(days=30),
            meta={"plan_code": request.plan_code, **request.metadata},
        )
        session.add(payment)
        session.flush()
        logger.info(
            "Payment intent recorded: user=%s amount=EGP %.2f (awaiting confirmation)",
            request.user.email, request.amount_egp,
        )
        return CheckoutResult(
            ok=True, payment=payment, redirect_url=None, requires_external_action=True,
            message=(
                "Your subscription request has been recorded. No card has been charged: "
                "no payment gateway is connected to this deployment yet. An administrator "
                "will confirm your payment and activate your subscription."
            ),
        )

    def status_note(self) -> str:
        return (
            "No payment gateway is connected. Checkout records intent only and an "
            "administrator activates the subscription manually. Connect an Egyptian "
            "gateway by implementing PaymentProvider in backend/billing/payments.py."
        )


class UnconfiguredGatewayProvider(PaymentProvider):
    """Placeholder for a named gateway whose credentials are absent.

    Refuses checkout rather than falling back to something that looks like it
    worked — a silent downgrade is how a business ends up believing it was paid.
    """

    name = "gateway"
    display_name = "Payment gateway (not configured)"

    def is_configured(self) -> bool:
        return bool(get_settings().payment_api_key)

    def create_checkout(self, session: Session, request: CheckoutRequest) -> CheckoutResult:
        return CheckoutResult(
            ok=False,
            error=(
                "The payment gateway is selected but has no credentials. Set "
                "EGX_PAYMENT_API_KEY, or switch EGX_PAYMENT_PROVIDER to 'manual'."
            ),
        )

    def status_note(self) -> str:
        return "Gateway selected but EGX_PAYMENT_API_KEY is not set. Checkout is disabled."


_REGISTRY: dict[str, type[PaymentProvider]] = {
    "manual": ManualPaymentProvider,
    "gateway": UnconfiguredGatewayProvider,
}


def register_provider(name: str, cls: type[PaymentProvider]) -> None:
    """Register a real gateway implementation."""
    _REGISTRY[name.lower()] = cls


def get_payment_provider() -> PaymentProvider:
    name = (get_settings().payment_provider or "manual").lower()
    return _REGISTRY.get(name, ManualPaymentProvider)()


def payment_status() -> dict[str, Any]:
    provider = get_payment_provider()
    return {
        "provider": provider.name,
        "display_name": provider.display_name,
        "configured": provider.is_configured(),
        "processes_payments": provider.processes_payments,
        "note": provider.status_note(),
    }
