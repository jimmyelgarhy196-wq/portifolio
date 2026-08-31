"""Shared FastAPI dependencies and template context."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.schemas import TEMPLATE_FILTERS, uses_synthetic_data
from backend.core.config import FRONTEND_DIR, get_settings
from backend.core.database import get_db
from backend.data.models import Alert, Portfolio
from backend.data.universe import universe_status

templates = Jinja2Templates(directory=str(FRONTEND_DIR / "templates"))
templates.env.filters.update(TEMPLATE_FILTERS)
templates.env.globals["today"] = date.today


def metric_of(analysis: Any, name: str) -> Any:
    """A fundamental metric by name, or None. Templates must never guess."""
    snapshot = getattr(analysis, "fundamental", None)
    if snapshot is None:
        return None
    metric = snapshot.metrics.get(name)
    return metric if (metric is not None and metric.available) else None


def metric_text(analysis: Any, name: str, dash: str = "N/A") -> str:
    """A metric formatted for display, or an explicit N/A. Never a zero."""
    metric = metric_of(analysis, name)
    return metric.formatted() if metric is not None else dash


def metric_value(analysis: Any, name: str) -> float | None:
    metric = metric_of(analysis, name)
    return metric.value if metric is not None else None


templates.env.globals.update({
    "metric_of": metric_of,
    "metric_text": metric_text,
    "metric_value": metric_value,
})


def base_context(request: Request, session: Session, **extra: Any) -> dict[str, Any]:
    """Context every page needs: banners, nav state, header stats."""
    from backend.portfolio.paper_trading import get_or_create_portfolio, mark_to_market

    settings = get_settings()
    synthetic = uses_synthetic_data(session)
    status = universe_status(session)

    portfolio = session.scalar(select(Portfolio).order_by(Portfolio.portfolio_id))
    header: dict[str, Any] = {
        "portfolio_value": None, "total_return": None,
        "cash_weight": None, "currency": settings.portfolio_currency,
    }
    if portfolio is not None:
        state = mark_to_market(session, portfolio)
        header.update({
            "portfolio_value": state["total_value"],
            "total_return": state["total_return"],
            "cash_weight": state["cash_weight"],
            "currency": portfolio.currency,
        })

    new_alerts = session.scalar(
        select(func.count()).select_from(Alert).where(Alert.status == "NEW")
    ) or 0

    context: dict[str, Any] = {
        "request": request,
        "settings": settings,
        "synthetic": synthetic,
        "universe_status": status,
        "header": header,
        "new_alerts": new_alerts,
        "ai_enabled": settings.ai_enabled,
        "active": "",
    }
    context.update(extra)
    return context


def render(request: Request, name: str, context: dict[str, Any], **kwargs: Any):
    """Render a template using Starlette's request-first signature."""
    return templates.TemplateResponse(request, name, context, **kwargs)


def db_session() -> Session:
    """Alias so routers read clearly."""
    return Depends(get_db)


# ---------------------------------------------------------------------------
# GMG context
# ---------------------------------------------------------------------------
def brand() -> dict[str, str]:
    from backend.core.config import (
        BRAND_COMPANY, BRAND_PRODUCT, BRAND_SHORT, BRAND_TAGLINE,
    )

    return {
        "company": BRAND_COMPANY, "product": BRAND_PRODUCT,
        "short": BRAND_SHORT, "tagline": BRAND_TAGLINE,
    }


def gmg_context(request: Request, session: Session, **extra: Any) -> dict[str, Any]:
    """Context for every GMG page: who is viewing, what they may see, and the
    honest state of the market data behind the screen.

    The data badge is computed here rather than in each template so no page can
    forget to say where its numbers came from.
    """
    from backend.api.auth_deps import get_viewer
    from backend.billing.subscriptions import current_plan
    from backend.market.status import market_state

    settings = get_settings()
    viewer = get_viewer(request, session)

    context: dict[str, Any] = {
        "request": request,
        "settings": settings,
        "brand": brand(),
        "viewer": viewer,
        "user": viewer.user,
        "entitled": viewer.entitled,
        "entitlement": viewer.entitlement,
        "csrf_token": viewer.csrf,
        "market": market_state(),
        "plan": current_plan().to_dict(),
        "synthetic": uses_synthetic_data(session),
        "active": "",
        "now": datetime.now(timezone.utc),
        "year": date.today().year,
    }
    context.update(extra)
    return context


def data_state(session: Session, tickers: list[str] | None = None) -> dict[str, Any]:
    """The banner describing where the prices on this page came from."""
    from backend.market.quotes import quote_freshness
    from backend.data.saas_models import Quote

    stmt = select(Quote)
    if tickers:
        stmt = stmt.where(Quote.ticker.in_([t.upper() for t in tickers]))
    rows = list(session.execute(stmt.limit(600)).scalars().all())
    if not rows:
        return {
            "badge": "NO DATA", "tone": "none", "is_demo": False,
            "message": (
                "N/A — data unavailable. No market-data provider is currently "
                "returning quotes."
            ),
        }
    if any(r.is_demo for r in rows):
        return {
            "badge": "DEMO DATA", "tone": "demo", "is_demo": True,
            "message": (
                "DEMO DATA — NOT REAL-TIME. Prices on this page are generated for "
                "demonstration and are not real market prices."
            ),
        }
    badges = {quote_freshness(r)["badge"] for r in rows}
    if "END OF DAY" in badges:
        return {"badge": "END OF DAY", "tone": "delayed", "is_demo": False,
                "message": "End-of-day prices from stored exchange data. Not a live feed."}
    if "DELAYED" in badges:
        delay = max((r.delayed_minutes or 0) for r in rows)
        return {"badge": "DELAYED", "tone": "delayed", "is_demo": False,
                "message": f"Prices are delayed by up to {delay} minutes under the market-data licence in force."}
    return {"badge": "LIVE", "tone": "live", "is_demo": False,
            "message": "Real-time prices from the configured licensed market-data provider."}
