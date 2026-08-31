"""Email delivery abstraction.

Three providers behind one interface:

* ``console`` (default in development) — writes the message to the log and
  records it in ``email_log``. Nothing leaves the machine.
* ``smtp`` — real delivery through a configured SMTP server.
* ``none`` — records the attempt as SKIPPED and sends nothing.

Every attempt is written to ``email_log`` whatever the outcome, so a missing
verification email can be diagnosed from the admin panel rather than guessed at.
A send failure never breaks the request that triggered it: signup succeeds even
if the mail server is down, and the user can request another link.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from backend.core.config import BRAND_COMPANY, BRAND_PRODUCT, get_settings
from backend.core.logging_config import get_logger
from backend.data.saas_models import EmailLog, User

logger = get_logger(__name__)


@dataclass
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str | None = None
    template: str = "generic"


@dataclass
class SendResult:
    ok: bool
    provider: str
    status: str            # SENT | FAILED | SKIPPED
    error: str | None = None


class EmailProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def send(self, message: EmailMessage) -> SendResult:
        ...

    def is_configured(self) -> bool:
        return True

    def configuration_note(self) -> str:
        return ""


class ConsoleEmailProvider(EmailProvider):
    """Logs the message instead of sending it. The development default."""

    name = "console"

    def send(self, message: EmailMessage) -> SendResult:
        logger.info(
            "EMAIL (console) to=%s subject=%s\n%s\n%s",
            message.to, message.subject, "-" * 68, message.text,
        )
        return SendResult(ok=True, provider=self.name, status="SENT")

    def configuration_note(self) -> str:
        return (
            "Development provider: emails are written to the server log and "
            "recorded in the email log, never delivered. Set EGX_EMAIL_PROVIDER=smtp "
            "with SMTP credentials to send real mail."
        )


class SmtpEmailProvider(EmailProvider):
    name = "smtp"

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.smtp_host and s.email_from)

    def configuration_note(self) -> str:
        if self.is_configured():
            s = get_settings()
            return f"SMTP {s.smtp_host}:{s.smtp_port} as {s.email_from}"
        return "SMTP is selected but EGX_SMTP_HOST / EGX_EMAIL_FROM are not set."

    def send(self, message: EmailMessage) -> SendResult:
        import smtplib
        from email.message import EmailMessage as MimeMessage

        s = get_settings()
        if not self.is_configured():
            return SendResult(False, self.name, "FAILED", self.configuration_note())

        mime = MimeMessage()
        mime["Subject"] = message.subject
        mime["From"] = f"{s.email_from_name} <{s.email_from}>"
        mime["To"] = message.to
        mime.set_content(message.text)
        if message.html:
            mime.add_alternative(message.html, subtype="html")

        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                if s.smtp_user:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(mime)
            return SendResult(True, self.name, "SENT")
        except Exception as exc:   # noqa: BLE001 - delivery must never break a request
            logger.warning("SMTP delivery failed: %s", exc)
            return SendResult(False, self.name, "FAILED", str(exc)[:500])


class NullEmailProvider(EmailProvider):
    name = "none"

    def send(self, message: EmailMessage) -> SendResult:
        return SendResult(True, self.name, "SKIPPED", "Email delivery is disabled.")

    def configuration_note(self) -> str:
        return "Email delivery is switched off. No messages are sent."


def get_provider() -> EmailProvider:
    name = (get_settings().email_provider or "console").lower()
    return {
        "console": ConsoleEmailProvider, "smtp": SmtpEmailProvider,
        "none": NullEmailProvider,
    }.get(name, ConsoleEmailProvider)()


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
FOOTER = f"""
—
{BRAND_PRODUCT}
{BRAND_COMPANY}

{BRAND_PRODUCT} provides market research, analytical tools and educational
information. Nothing in this message is investment advice, a recommendation to
transact, or a promise of investment performance.
"""


def _wrap(body: str) -> str:
    return body.rstrip() + "\n" + FOOTER


def render(template: str, **ctx: Any) -> tuple[str, str]:
    """Return ``(subject, text)`` for a named template."""
    name = ctx.get("name") or "there"
    url = ctx.get("url", "")

    if template == "welcome":
        return (
            f"Welcome to {BRAND_PRODUCT}",
            _wrap(f"""Hello {name},

Your {BRAND_PRODUCT} account is ready.

{BRAND_PRODUCT} gives you EGX market data, fundamental and technical analysis,
AI-generated research, a stock screener, watchlists and portfolio tracking.

Sign in: {ctx.get('login_url', '')}

A note on what this service is: we provide research and analytics. We do not
hold your money, hold securities, or place trades on your behalf."""),
        )

    if template == "verify_email":
        return (
            f"Verify your email — {BRAND_PRODUCT}",
            _wrap(f"""Hello {name},

Confirm your email address to activate your {BRAND_PRODUCT} account:

{url}

This link expires in 48 hours and can be used once.

If you did not create an account, you can ignore this message."""),
        )

    if template == "reset_password":
        return (
            f"Reset your password — {BRAND_PRODUCT}",
            _wrap(f"""Hello {name},

Use this link to set a new password:

{url}

The link expires in 60 minutes and can be used once. Resetting your password
signs you out of every device.

If you did not request this, no action is needed — your password is unchanged."""),
        )

    if template == "subscription_confirmed":
        return (
            f"Your {BRAND_PRODUCT} subscription is active",
            _wrap(f"""Hello {name},

Your subscription to {ctx.get('plan_name', BRAND_PRODUCT)} is active.

Plan:    {ctx.get('plan_name', '')}
Price:   EGP {ctx.get('price', '')} per {ctx.get('interval', 'month')}
Renews:  {ctx.get('period_end', '—')}

Manage your subscription at {ctx.get('billing_url', '')}"""),
        )

    if template == "payment_succeeded":
        return (
            f"Payment received — {BRAND_PRODUCT}",
            _wrap(f"""Hello {name},

We have recorded your payment of EGP {ctx.get('amount', '')}.

Reference: {ctx.get('reference', '—')}
Period:    {ctx.get('period', '—')}

Your invoice history is at {ctx.get('billing_url', '')}"""),
        )

    if template == "payment_failed":
        return (
            f"Payment problem — {BRAND_PRODUCT}",
            _wrap(f"""Hello {name},

We could not process your payment of EGP {ctx.get('amount', '')}.

Reason: {ctx.get('reason', 'The payment was not completed.')}

Your access continues until {ctx.get('period_end', 'the end of the current period')}.
Update your payment details at {ctx.get('billing_url', '')}"""),
        )

    if template == "subscription_cancelled":
        return (
            f"Subscription cancelled — {BRAND_PRODUCT}",
            _wrap(f"""Hello {name},

Your subscription has been cancelled and will not renew.

You keep full access until {ctx.get('period_end', 'the end of your paid period')}.
After that your account stays open with limited access, and you can resubscribe
at any time.

We would value knowing why you left: {ctx.get('contact_url', '')}"""),
        )

    if template == "weekly_report":
        return (
            f"GMG Weekly EGX Intelligence — {ctx.get('period_end', '')}",
            _wrap(f"""Hello {name},

Your weekly EGX intelligence report is ready.

{ctx.get('summary', '')}

Read the full report: {url}"""),
        )

    if template == "alert":
        return (
            f"Alert: {ctx.get('ticker', '')} — {BRAND_PRODUCT}",
            _wrap(f"""Hello {name},

An alert you set has triggered.

{ctx.get('message', '')}

Ticker:    {ctx.get('ticker', '')}
Condition: {ctx.get('condition', '')}
Price:     {ctx.get('price', '')}
Time:      {ctx.get('time', '')}

{ctx.get('data_note', '')}

View the stock: {url}

This is a notification that a condition you configured was met. It is not a
recommendation to buy or sell."""),
        )

    if template == "contact_message":
        return (
            f"Website enquiry: {ctx.get('subject', 'no subject')}",
            _wrap(f"""A message was sent through the {BRAND_PRODUCT} contact form.

From:    {ctx.get('name', 'not given')}
Email:   {ctx.get('from_email', 'not given')}
Subject: {ctx.get('subject', 'not given')}

{ctx.get('message', '')}

Reply to the sender directly at the address above. Treat the content as
unverified: it was submitted by a member of the public."""),
        )

    return (ctx.get("subject", BRAND_PRODUCT), _wrap(ctx.get("body", "")))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def send_email(
    session: Session, *, to: str, template: str, user: User | None = None, **ctx: Any
) -> SendResult:
    """Render, send and record an email. Never raises."""
    subject, text = render(template, **ctx)
    message = EmailMessage(to=to, subject=subject, text=text, template=template)
    provider = get_provider()

    try:
        result = provider.send(message)
    except Exception as exc:   # noqa: BLE001
        logger.exception("Email provider raised")
        result = SendResult(False, provider.name, "FAILED", str(exc)[:500])

    session.add(EmailLog(
        user_id=user.id if user else None,
        to_email=to, template=template, subject=subject[:255],
        provider=result.provider, status=result.status, error=result.error,
    ))
    return result


def email_status() -> dict[str, Any]:
    """Provider health for the admin panel."""
    provider = get_provider()
    return {
        "provider": provider.name,
        "configured": provider.is_configured(),
        "note": provider.configuration_note(),
        "delivers": provider.name == "smtp" and provider.is_configured(),
    }
