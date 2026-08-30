"""Shared helpers for API responses and template rendering."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.data.models import PriceBar


def uses_synthetic_data(session: Session) -> bool:
    """Whether any stored market data came from the synthetic provider.

    Drives the permanent UI warning banner. Checked against actual stored rows
    rather than configuration, because data seeded earlier remains synthetic
    however the flag is set now.
    """
    row = session.scalar(
        select(PriceBar.source).where(PriceBar.source.like("SYNTHETIC%")).limit(1)
    )
    return row is not None


def score_class(value: float | None) -> str:
    """CSS class for a 0-100 score chip."""
    if value is None:
        return "score-na"
    if value >= 90:
        return "score-90"
    if value >= 75:
        return "score-75"
    if value >= 60:
        return "score-60"
    if value >= 45:
        return "score-45"
    return "score-0"


def fmt_num(value: Any, digits: int = 2, dash: str = "—") -> str:
    if value is None:
        return dash
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return dash


def fmt_pct(value: Any, digits: int = 1, dash: str = "—", signed: bool = False) -> str:
    if value is None:
        return dash
    try:
        return f"{float(value):{'+' if signed else ''}.{digits}%}"
    except (TypeError, ValueError):
        return dash


def fmt_money(value: Any, currency: str = "EGP", digits: int = 0, dash: str = "—") -> str:
    if value is None:
        return dash
    try:
        return f"{currency} {float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return dash


def fmt_x(value: Any, digits: int = 2, dash: str = "—") -> str:
    if value is None:
        return dash
    try:
        return f"{float(value):,.{digits}f}x"
    except (TypeError, ValueError):
        return dash


def direction_class(value: Any) -> str:
    if value is None:
        return "flat"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "flat"
    if number > 0:
        return "up"
    if number < 0:
        return "down"
    return "flat"


TEMPLATE_FILTERS = {
    "fmt_num": fmt_num,
    "fmt_pct": fmt_pct,
    "fmt_money": fmt_money,
    "fmt_x": fmt_x,
    "score_class": score_class,
    "direction_class": direction_class,
}
