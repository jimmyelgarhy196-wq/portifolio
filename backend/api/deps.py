"""Shared FastAPI dependencies and template context."""
from __future__ import annotations

from datetime import date
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
