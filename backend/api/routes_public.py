"""Public pages: landing, pricing, legal, contact, and data-source transparency.

The legal pages are written for GMG's actual business model — a subscription
research and information service. They deliberately do **not** claim any
licence, registration or authorisation from the Egyptian Financial Regulatory
Authority or any other regulator, because none has been obtained. See
``LEGAL_REVIEW_TODO`` below: these documents are drafts and require review by
qualified Egyptian counsel before the service is offered commercially.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.auth_deps import (
    Viewer,
    client_ip,
    enforce_csrf,
    form_token,
    get_viewer,
    rate_limit,
    set_form_cookie,
)
from backend.api.deps import gmg_context, render
from backend.api.routes_auth import flash_from
from backend.billing.payments import payment_status
from backend.billing.subscriptions import current_plan
from backend.core.config import get_settings
from backend.core.database import get_db
from backend.core.logging_config import get_logger
from backend.data.saas_models import DataSourceRecord
from backend.market.quotes import provider_chain
from backend.notify.email_service import email_status, send_email

logger = get_logger(__name__)
router = APIRouter()

#: Date the legal documents were last revised. Shown on every legal page.
LEGAL_VERSION = date(2026, 8, 31)

# ---------------------------------------------------------------------------
# INTERNAL TODO — NOT LEGAL ADVICE, NOT A COMPLETED REVIEW
#
# Have Egyptian legal counsel review all legal documents and confirm whether
# any FRA, Central Bank of Egypt, consumer-protection, data-protection (Law
# 151/2020), tax, e-commerce or other authorisation or registration
# requirements apply to the exact GMG business model — a paid subscription
# providing market information, analytics and AI-generated research commentary,
# with no client money, no custody of securities, no order execution, no
# discretionary management and no personalised advice.
#
# These documents were drafted to describe that model accurately. They have NOT
# been reviewed by a qualified Egyptian lawyer. Do not open the service to
# paying customers until that review is complete.
# ---------------------------------------------------------------------------
LEGAL_REVIEW_TODO = (
    "Have Egyptian legal counsel review all legal documents and confirm whether any FRA, "
    "Central Bank, consumer-protection, data-protection, tax, e-commerce or other "
    "authorization/registration requirements apply to the exact GMG business model."
)

LEGAL_PAGES: dict[str, tuple[str, str]] = {
    "terms": ("Terms of Service", "gmg/legal/terms.html"),
    "privacy": ("Privacy Policy", "gmg/legal/privacy.html"),
    "disclaimer": ("Investment Disclaimer", "gmg/legal/disclaimer.html"),
    "risk-disclosure": ("Risk Disclosure", "gmg/legal/risk_disclosure.html"),
    "subscription-terms": ("Subscription Terms", "gmg/legal/subscription_terms.html"),
    "refund-policy": ("Refund Policy", "gmg/legal/refund_policy.html"),
    "cookies": ("Cookie Policy", "gmg/legal/cookies.html"),
}


def _ctx(request: Request, db: Session, **extra: Any) -> dict[str, Any]:
    context = gmg_context(request, db, **extra)
    context.setdefault("flash", flash_from(request))
    context.setdefault("legal_version", LEGAL_VERSION)
    return context


# ---------------------------------------------------------------------------
# Landing and marketing
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)):
    viewer = get_viewer(request, db)
    if viewer.is_authenticated:
        return RedirectResponse("/market", status_code=303)
    return render(request, "gmg/landing.html", _ctx(request, db, plan=current_plan().to_dict()))


@router.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request, db: Session = Depends(get_db)):
    return render(request, "gmg/pricing.html", _ctx(
        request, db, plan=current_plan().to_dict(), payments=payment_status(),
    ))


@router.get("/about", response_class=HTMLResponse)
def about(request: Request, db: Session = Depends(get_db)):
    return render(request, "gmg/about.html", _ctx(request, db))


# ---------------------------------------------------------------------------
# Data-source transparency
# ---------------------------------------------------------------------------
@router.get("/data-sources", response_class=HTMLResponse)
def data_sources(request: Request, db: Session = Depends(get_db)):
    """Exactly where every number on the platform comes from."""
    from backend.data.providers.registry import provider_status

    records = list(db.execute(
        select(DataSourceRecord).order_by(DataSourceRecord.kind, DataSourceRecord.name)
    ).scalars().all())
    quote_providers = [
        {"name": p.display_name, "is_demo": p.is_demo, "available": p.is_available(),
         "delay": p.delayed_minutes, "note": p.status_note(),
         "requires_credentials": p.requires_credentials}
        for p in provider_chain(db)
    ]
    return render(request, "gmg/data_sources.html", _ctx(
        request, db, records=records, quote_providers=quote_providers,
        market_providers=provider_status(), email=email_status(),
        payments=payment_status(),
    ))


# ---------------------------------------------------------------------------
# Legal
# ---------------------------------------------------------------------------
def _make_legal_route(slug: str, title: str, template: str):
    """One explicit route per document.

    A single catch-all ``/{slug}`` route would shadow every other page in the
    application, so each legal path is registered by name.
    """

    def view(request: Request, db: Session = Depends(get_db)):
        return render(request, template, _ctx(
            request, db, legal_title=title, legal_slug=slug,
            plan=current_plan().to_dict(),
        ))

    view.__name__ = f"legal_{slug.replace('-', '_')}"
    return view


for _slug, (_title, _template) in LEGAL_PAGES.items():
    router.add_api_route(
        f"/{_slug}", _make_legal_route(_slug, _title, _template),
        methods=["GET"], response_class=HTMLResponse, name=f"legal_{_slug}",
    )


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------
def _render_contact(request: Request, db: Session, *, status_code: int = 200, **extra: Any):
    """Render the contact form with the CSRF token it posts back."""
    token, fresh = form_token(request)
    context = _ctx(request, db, **extra)
    context["csrf_token"] = token
    response = render(request, "gmg/contact.html", context, status_code=status_code)
    if fresh is not None:
        set_form_cookie(response, fresh)
    return response


@router.get("/contact", response_class=HTMLResponse)
def contact_form(request: Request, db: Session = Depends(get_db)):
    return _render_contact(request, db, form={})


@router.post("/contact", response_class=HTMLResponse)
def contact_submit(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(""),
    email: str = Form(""),
    subject: str = Form(""),
    message: str = Form(""),
    db: Session = Depends(get_db),
):
    enforce_csrf(request, csrf_token)
    ip = client_ip(request)
    if not rate_limit(f"contact:{ip}", limit=4, window_seconds=900):
        return _render_contact(
            request, db, status_code=429,
            form={"name": name, "email": email, "subject": subject},
            flash={"kind": "error", "message":
                   "Too many messages from this network. Please try again later."})

    if not (email.strip() and message.strip()):
        return _render_contact(
            request, db, status_code=400,
            form={"name": name, "email": email, "subject": subject},
            flash={"kind": "error", "message": "Please provide your email and a message."})

    settings = get_settings()
    destination = settings.admin_email or settings.email_from
    if destination:
        send_email(db, to=destination, template="contact_message",
            name=(name or "Not given")[:120], from_email=email.strip()[:255],
            subject=(subject or "Website enquiry")[:160], message=message[:4000],
        )
        db.commit()
    else:
        # No destination configured: say so rather than pretending it was sent.
        logger.warning("Contact form submitted but no admin email is configured.")
        return _render_contact(
            request, db, form={},
            flash={"kind": "warn", "message":
                   "This site has no contact address configured yet, so your message could "
                   "not be delivered. Please email us directly."})

    return _render_contact(
        request, db, form={},
        flash={"kind": "ok", "message":
               "Thank you — your message has been sent. We reply within two business days."})


#: The brand mark, served as an SVG so browsers requesting /favicon.ico get a
#: real icon instead of a 404 in every visitor's console.
FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='14' fill='#d4af37'/>"
    "<text x='32' y='43' font-size='26' font-weight='bold' text-anchor='middle' "
    "font-family='sans-serif' fill='#10131a'>G</text></svg>"
)


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Any:
    from fastapi.responses import Response

    return Response(
        content=FAVICON_SVG, media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots() -> str:
    """Marketing pages are indexable; the application and account areas are not."""
    base = get_settings().base_url.rstrip("/")
    return "\n".join([
        "User-agent: *",
        "Allow: /$",
        "Allow: /pricing",
        "Allow: /about",
        "Allow: /contact",
        "Allow: /data-sources",
        "Allow: /terms",
        "Allow: /privacy",
        "Allow: /disclaimer",
        "Allow: /risk-disclosure",
        "Allow: /subscription-terms",
        "Allow: /refund-policy",
        "Allow: /cookies",
        "Disallow: /market",
        "Disallow: /stock/",
        "Disallow: /stocks",
        "Disallow: /screener",
        "Disallow: /account",
        "Disallow: /admin",
        "Disallow: /api/",
        "Disallow: /login",
        "Disallow: /signup",
        "",
        f"Sitemap: {base}/sitemap.xml",
    ])


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> Any:
    from fastapi.responses import Response

    base = get_settings().base_url.rstrip("/")
    paths = ["/", "/pricing", "/about", "/contact", "/data-sources", *(
        f"/{slug}" for slug in LEGAL_PAGES
    )]
    urls = "".join(
        f"<url><loc>{base}{path}</loc><lastmod>{LEGAL_VERSION.isoformat()}</lastmod></url>"
        for path in paths
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
    return Response(content=xml, media_type="application/xml")
