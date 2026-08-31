"""EGX market session state.

The EGX trades Sunday to Thursday, roughly 10:00–14:30 Cairo time. Every quote
carries the session state it was captured in, so a price shown at 22:00 is
labelled CLOSED rather than implying a live market.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any

from backend.core.config import get_settings

#: Cairo is UTC+2 (EET). Egypt reintroduced DST in 2023 (UTC+3 in summer), so
#: this is an approximation; a provider-supplied session state always wins.
CAIRO_OFFSET_HOURS = 2


class MarketStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PRE_OPEN = "PRE_OPEN"
    HOLIDAY = "HOLIDAY"
    UNKNOWN = "UNKNOWN"

    @property
    def label(self) -> str:
        return {
            MarketStatus.OPEN: "Market Open",
            MarketStatus.CLOSED: "Market Closed",
            MarketStatus.PRE_OPEN: "Pre-Open",
            MarketStatus.HOLIDAY: "Market Holiday",
            MarketStatus.UNKNOWN: "Status Unknown",
        }[self]

    @property
    def is_live(self) -> bool:
        return self is MarketStatus.OPEN


def cairo_now(at: datetime | None = None) -> datetime:
    ref = at or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref.astimezone(timezone(timedelta(hours=CAIRO_OFFSET_HOURS)))


def _parse_time(text: str, fallback: time) -> time:
    try:
        hh, mm = str(text).split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return fallback


@dataclass
class SessionState:
    status: MarketStatus
    local_time: datetime
    opens_at: time
    closes_at: time
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "label": self.status.label,
            "is_live": self.status.is_live,
            "local_time": self.local_time.strftime("%d %b %Y, %H:%M EET"),
            "session": f"{self.opens_at:%H:%M}–{self.closes_at:%H:%M} EET, Sunday–Thursday",
            "note": self.note,
        }


def market_state(at: datetime | None = None) -> SessionState:
    """Current EGX session state, derived from the clock and the trading week.

    This is a calendar approximation: it does not know Egyptian public holidays.
    A provider that reports its own session state overrides this.
    """
    settings = get_settings()
    local = cairo_now(at)
    opens = _parse_time(settings.market_open_time, time(10, 0))
    closes = _parse_time(settings.market_close_time, time(14, 30))

    if local.weekday() not in settings.trading_weekdays:
        return SessionState(
            MarketStatus.CLOSED, local, opens, closes,
            note="The EGX trades Sunday to Thursday.",
        )

    current = local.time()
    if current < opens:
        minutes = (
            datetime.combine(local.date(), opens) - datetime.combine(local.date(), current)
        ).seconds // 60
        return SessionState(
            MarketStatus.PRE_OPEN, local, opens, closes,
            note=f"Opens in {minutes // 60}h {minutes % 60}m.",
        )
    if current > closes:
        return SessionState(
            MarketStatus.CLOSED, local, opens, closes, note="Today's session has ended.",
        )
    return SessionState(MarketStatus.OPEN, local, opens, closes)
