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


# ---------------------------------------------------------------------------
# GMG presentation filters
# ---------------------------------------------------------------------------
#: Shown wherever a figure genuinely is not available. Never a zero, never a
#: dash that could be mistaken for a real value of nothing.
NA_TEXT = "N/A — data unavailable"


def fmt_price(value: Any, currency: str = "EGP", dash: str = NA_TEXT) -> str:
    """EGX prices carry two decimals; sub-EGP 1 tickers carry three."""
    if value is None:
        return dash
    try:
        number = float(value)
    except (TypeError, ValueError):
        return dash
    digits = 3 if abs(number) < 1 else 2
    return f"{currency} {number:,.{digits}f}" if currency else f"{number:,.{digits}f}"


def fmt_compact(value: Any, dash: str = "—", digits: int = 2) -> str:
    """1_234_567 -> 1.23M. Used for volume and traded value."""
    if value is None:
        return dash
    try:
        number = float(value)
    except (TypeError, ValueError):
        return dash
    sign = "-" if number < 0 else ""
    number = abs(number)
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if number >= cutoff:
            return f"{sign}{number / cutoff:,.{digits}f}{suffix}"
    return f"{sign}{number:,.0f}"


def fmt_dt(value: Any, fmt: str = "%d %b %Y, %H:%M", dash: str = "—") -> str:
    if value is None:
        return dash
    try:
        return value.strftime(fmt)
    except AttributeError:
        return str(value)


def fmt_date(value: Any, fmt: str = "%d %b %Y", dash: str = "—") -> str:
    return fmt_dt(value, fmt, dash)


def badge_class(badge: str | None) -> str:
    """Maps a freshness badge onto its CSS class."""
    return {
        "LIVE": "badge-live", "DELAYED": "badge-delayed", "END OF DAY": "badge-eod",
        "DEMO DATA": "badge-demo", "NO DATA": "badge-none",
    }.get((badge or "").upper(), "badge-muted")


def rating_class(rating: str | None) -> str:
    if not rating:
        return "rating-na"
    return "rating-" + rating.strip().lower().replace(" ", "_").replace("-", "_")


def sign_class(value: Any) -> str:
    """Alias of direction_class, named for how templates read."""
    return direction_class(value)


TEMPLATE_FILTERS.update({
    "fmt_price": fmt_price,
    "fmt_compact": fmt_compact,
    "fmt_dt": fmt_dt,
    "fmt_date": fmt_date,
    "badge_class": badge_class,
    "rating_class": rating_class,
    "sign_class": sign_class,
})
