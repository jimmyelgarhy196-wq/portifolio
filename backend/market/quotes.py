"""Quote provider interface, demo provider, and the quote service.

Architecture, as required: **provider → backend → database/cache → frontend.**
Nothing in the frontend talks to a market-data source directly, and no page
computes a price.

On honesty, which is the point of this module:

* A quote is only ever labelled live if a provider that actually delivers
  real-time data produced it. :attr:`QuoteProvider.is_demo` and
  ``delayed_minutes`` travel with every quote into the database and out to the
  UI.
* The demo provider exists so the platform is operable before a market-data
  contract is signed. Everything it produces is stamped ``is_demo=True`` and
  rendered behind a "DEMO DATA — NOT REAL-TIME" badge. It is deterministic per
  ticker and day, so the same demo price appears on every reload rather than
  flickering like a live feed.
* TradingView is not a data source here. It is not scraped, and no claim is
  made that it offers an unrestricted real-time API. Its charting library may
  be embedded for visualisation under its own licence; that is a rendering
  choice, not a data feed.
"""
from __future__ import annotations

import abc
import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.core.logging_config import EVENT_PROVIDER_FAILURE, get_logger, log_event
from backend.data.providers.base import ProviderUnavailable
from backend.data.saas_models import DataSourceRecord, Quote
from backend.market.live_providers import PRESETS, RestQuoteProvider, load_vendor_spec
from backend.market.status import MarketStatus, market_state

logger = get_logger(__name__)

DEMO_LABEL = "DEMO DATA — NOT REAL-TIME"


@dataclass
class QuoteData:
    """One market snapshot, with its provenance attached."""

    ticker: str
    price: float | None = None
    previous_close: float | None = None
    open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: float | None = None
    turnover: float | None = None
    trades: int | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    market_cap: float | None = None
    currency: str = "EGP"
    quote_time: datetime | None = None
    source: str = "UNKNOWN"
    delayed_minutes: int = 0
    market_status: str = MarketStatus.UNKNOWN.value
    is_demo: bool = False

    @property
    def change(self) -> float | None:
        if self.price is None or self.previous_close is None:
            return None
        return self.price - self.previous_close

    @property
    def change_pct(self) -> float | None:
        if self.price is None or not self.previous_close:
            return None
        return (self.price - self.previous_close) / self.previous_close

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
            "source": self.source, "delayed_minutes": self.delayed_minutes,
            "market_status": self.market_status, "is_demo": self.is_demo,
        }


class QuoteProvider(abc.ABC):
    """Interface a market-data vendor implements."""

    name = "base"
    display_name = "Base"
    is_demo = False
    requires_credentials = False
    #: 0 means real time. Anything else is surfaced to the user verbatim.
    delayed_minutes = 0

    @abc.abstractmethod
    def get_quotes(self, tickers: Sequence[str]) -> dict[str, QuoteData]:
        """Return quotes for the tickers it covers. Missing tickers are omitted,
        never invented."""

    def get_quote(self, ticker: str) -> QuoteData | None:
        return self.get_quotes([ticker]).get(ticker.upper())

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str | None:
        return None

    def status_note(self) -> str:
        return ""


class DemoQuoteProvider(QuoteProvider):
    """Generates plausible but FICTIONAL quotes so the platform is operable
    before a data contract exists.

    Deterministic per (ticker, date): the same demo price shows on every reload
    within a day, which keeps it from imitating a live tape. Every field it
    produces is stamped ``is_demo=True``.
    """

    name = "demo"
    display_name = "Demo (not real market data)"
    is_demo = True
    delayed_minutes = 0

    def __init__(self, session: Session | None = None) -> None:
        self._session = session

    def _seed(self, ticker: str, salt: str = "") -> float:
        """A stable pseudo-random number in [0, 1) from ticker + today."""
        key = f"{ticker.upper()}|{date.today().isoformat()}|{salt}"
        digest = hashlib.sha256(key.encode()).digest()
        return int.from_bytes(digest[:6], "big") / float(1 << 48)

    def _anchor_price(self, ticker: str) -> float:
        """Anchor to the last stored close when there is one, so demo quotes
        line up with stored history instead of contradicting it."""
        if self._session is not None:
            from backend.data.models import PriceBar

            bar = self._session.scalar(
                select(PriceBar).where(PriceBar.ticker == ticker.upper())
                .order_by(PriceBar.timestamp.desc())
            )
            if bar is not None and (bar.close or bar.adjusted_close):
                return float(bar.close or bar.adjusted_close)
        # No history: derive a stable level from the ticker itself.
        return round(5.0 + self._seed(ticker, "level") * 195.0, 2)

    def get_quotes(self, tickers: Sequence[str]) -> dict[str, QuoteData]:
        state = market_state()
        out: dict[str, QuoteData] = {}
        for raw in tickers:
            ticker = raw.upper()
            previous = self._anchor_price(ticker)
            # ±3.5% daily move, deterministic for the day.
            move = (self._seed(ticker, "move") - 0.5) * 0.07
            price = round(max(0.05, previous * (1 + move)), 2)
            spread = abs(price - previous) + price * 0.012 * self._seed(ticker, "range")
            high = round(max(price, previous) + spread * 0.6, 2)
            low = round(max(0.01, min(price, previous) - spread * 0.6), 2)
            open_price = round(low + (high - low) * self._seed(ticker, "open"), 2)
            volume = round(50_000 + self._seed(ticker, "vol") * 4_000_000)

            out[ticker] = QuoteData(
                ticker=ticker, price=price, previous_close=previous,
                open=open_price, day_high=high, day_low=low,
                volume=volume, turnover=round(volume * price, 2),
                trades=int(200 + self._seed(ticker, "trades") * 5_000),
                week52_high=round(price * (1.15 + self._seed(ticker, "hi") * 0.5), 2),
                week52_low=round(price * (0.60 + self._seed(ticker, "lo") * 0.25), 2),
                currency="EGP",
                quote_time=datetime.now(timezone.utc),
                source=f"DEMO:{self.name}",
                delayed_minutes=0,
                market_status=state.status.value,
                is_demo=True,
            )
        return out

    def status_note(self) -> str:
        return (
            "Demo provider. Prices are generated, not observed, and every quote is "
            "labelled accordingly. Connect a licensed EGX market-data provider to "
            "replace it."
        )


class StoredPriceQuoteProvider(QuoteProvider):
    """Builds quotes from the last two stored daily bars.

    Not a live feed and never presented as one: it reports the last close the
    database holds, with ``delayed_minutes`` set from the age of that bar. Useful
    when prices arrive by CSV import or an end-of-day feed.
    """

    name = "stored"
    display_name = "Stored end-of-day prices"
    is_demo = False

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_quotes(self, tickers: Sequence[str]) -> dict[str, QuoteData]:
        from backend.data.models import PriceBar

        state = market_state()
        out: dict[str, QuoteData] = {}
        for raw in tickers:
            ticker = raw.upper()
            bars = self._session.execute(
                select(PriceBar).where(PriceBar.ticker == ticker)
                .order_by(PriceBar.timestamp.desc()).limit(260)
            ).scalars().all()
            if not bars:
                continue
            latest = bars[0]
            close = latest.close if latest.close is not None else latest.adjusted_close
            if close is None:
                continue
            previous = None
            if len(bars) > 1:
                previous = bars[1].close if bars[1].close is not None else bars[1].adjusted_close

            highs = [b.high for b in bars if b.high is not None]
            lows = [b.low for b in bars if b.low is not None]
            age_minutes = int(
                (datetime.now(timezone.utc).date() - latest.timestamp).days * 24 * 60
            )
            is_demo = "SYNTHETIC" in (latest.source or "").upper()

            out[ticker] = QuoteData(
                ticker=ticker, price=close, previous_close=previous,
                open=latest.open, day_high=latest.high, day_low=latest.low,
                volume=latest.volume,
                turnover=(latest.volume * close) if latest.volume else None,
                week52_high=max(highs) if highs else None,
                week52_low=min(lows) if lows else None,
                currency="EGP",
                quote_time=datetime(
                    latest.timestamp.year, latest.timestamp.month, latest.timestamp.day,
                    tzinfo=timezone.utc,
                ),
                source=latest.source or "stored",
                delayed_minutes=max(0, age_minutes),
                market_status=state.status.value,
                is_demo=is_demo,
            )
        return out

    def status_note(self) -> str:
        return (
            "Serves the most recent stored daily bar. This is end-of-day data, "
            "labelled with its true age — not a real-time feed."
        )


class LicensedQuoteProvider(QuoteProvider):
    """A licensed EGX market-data vendor, called over its documented REST API.

    Two things must both be true before this provider will serve anything: a
    vendor must be named (``EGX_MARKET_DATA_VENDOR``, or a spec file) and a key
    must be present (``EGX_MARKET_DATA_API_KEY``). Either one alone is a
    misconfiguration, and a misconfiguration serves nothing.

    It never falls back to demo data. A silent downgrade would put fabricated
    prices behind a "live" label, which is the one failure this architecture
    exists to prevent — so when the vendor is down, the platform shows stale or
    unavailable, and says which.
    """

    name = "licensed"
    display_name = "Licensed EGX market data"
    requires_credentials = True

    def __init__(self, client: "RestQuoteProvider | None" = None) -> None:
        settings = get_settings()
        self.delayed_minutes = settings.quote_delay_minutes
        self._client = client
        self._spec = None
        self._config_error: str | None = None
        if client is not None:
            self._spec = client.spec
            self.display_name = f"{client.spec.display_name} (licensed feed)"
            self.delayed_minutes = client.delayed_minutes
            return
        try:
            self._spec = load_vendor_spec()
        except ProviderUnavailable as exc:
            self._config_error = str(exc)
        if self._spec is not None:
            self.display_name = f"{self._spec.display_name} (licensed feed)"

    # -- availability -------------------------------------------------------
    def is_available(self) -> bool:
        if self._client is not None:
            return True
        return bool(get_settings().market_data_api_key) and self._spec is not None

    def unavailable_reason(self) -> str | None:
        if self.is_available():
            return None
        if self._config_error:
            return self._config_error
        has_key = bool(get_settings().market_data_api_key)
        if self._spec is None and not has_key:
            return (
                "No live feed attached. Set EGX_MARKET_DATA_VENDOR (or "
                "EGX_MARKET_DATA_SPEC_PATH) and EGX_MARKET_DATA_API_KEY to go live."
            )
        if self._spec is None:
            return (
                "A market-data key is set but no vendor is named. Set "
                "EGX_MARKET_DATA_VENDOR to one of: "
                f"{', '.join(sorted(PRESETS))} — or point "
                "EGX_MARKET_DATA_SPEC_PATH at a vendor spec file."
            )
        return (
            f"Vendor {self._spec.display_name} is configured but "
            "EGX_MARKET_DATA_API_KEY is empty."
        )

    # -- fetch --------------------------------------------------------------
    def _build_client(self) -> "RestQuoteProvider":
        if self._client is None:
            assert self._spec is not None  # guarded by is_available()
            self._client = RestQuoteProvider(
                self._spec, get_settings().market_data_api_key,
                delayed_minutes=self.delayed_minutes,
            )
        return self._client

    def get_quotes(self, tickers: Sequence[str]) -> dict[str, QuoteData]:
        if not self.is_available():
            return {}
        client = self._build_client()
        state = market_state()
        raw = client.fetch(tickers)

        out: dict[str, QuoteData] = {}
        for ticker, values in raw.items():
            out[ticker] = QuoteData(
                ticker=ticker,
                source=client.spec.name,
                # Never derived from vendor payload: this path is real data by
                # construction, and the demo path is fake by construction.
                is_demo=False,
                delayed_minutes=self.delayed_minutes,
                market_status=state.status.value,
                **values,
            )
        return out

    def status_note(self) -> str:
        if not self.is_available():
            return self.unavailable_reason() or ""
        delay = self.delayed_minutes
        timing = "real time" if delay == 0 else f"delayed {delay} minutes"
        spec = self._spec or (self._client.spec if self._client else None)
        vendor = spec.display_name if spec else "vendor"
        return f"Live feed: {vendor}, {timing}, as configured by your licence."


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------
def build_quote_provider(session: Session) -> QuoteProvider:
    """Pick the quote provider: licensed if usable, then stored, then demo.

    Demo is last and only reached when nothing real is available — and when it
    is reached, everything it produces is labelled.
    """
    licensed = LicensedQuoteProvider()
    if licensed.is_available():
        return licensed

    from backend.data.models import PriceBar

    has_prices = session.scalar(select(PriceBar.id).limit(1)) is not None
    if has_prices:
        return StoredPriceQuoteProvider(session)
    return DemoQuoteProvider(session)


def provider_chain(session: Session) -> list[QuoteProvider]:
    """All providers in preference order, for the admin status view."""
    return [LicensedQuoteProvider(), StoredPriceQuoteProvider(session), DemoQuoteProvider(session)]


# ---------------------------------------------------------------------------
# Service: fetch, cache, read
# ---------------------------------------------------------------------------
def refresh_quotes(
    session: Session, tickers: Sequence[str], *, provider: QuoteProvider | None = None
) -> dict[str, Quote]:
    """Fetch quotes and upsert them into the cache. Returns stored rows."""
    provider = provider or build_quote_provider(session)
    try:
        fetched = provider.get_quotes([t.upper() for t in tickers])
    except NotImplementedError as exc:
        log_event(logger, EVENT_PROVIDER_FAILURE, str(exc), provider=provider.name)
        record_source_health(session, provider, error=str(exc))
        return {}
    except Exception as exc:  # noqa: BLE001
        log_event(
            logger, EVENT_PROVIDER_FAILURE,
            f"Quote fetch failed: {exc}", provider=provider.name,
        )
        record_source_health(session, provider, error=str(exc)[:500])
        return {}

    stored: dict[str, Quote] = {}
    for ticker, data in fetched.items():
        row = session.get(Quote, ticker)
        payload = {
            "price": data.price, "previous_close": data.previous_close,
            "change": data.change, "change_pct": data.change_pct,
            "open": data.open, "day_high": data.day_high, "day_low": data.day_low,
            "volume": data.volume, "turnover": data.turnover, "trades": data.trades,
            "week52_high": data.week52_high, "week52_low": data.week52_low,
            "market_cap": data.market_cap, "currency": data.currency,
            "quote_time": (
                data.quote_time.replace(tzinfo=None) if data.quote_time else None
            ),
            "retrieved_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "source": data.source, "delayed_minutes": data.delayed_minutes,
            "market_status": data.market_status, "is_demo": data.is_demo,
        }
        if row is None:
            row = Quote(ticker=ticker, **payload)
            session.add(row)
        else:
            for key, value in payload.items():
                setattr(row, key, value)
        stored[ticker] = row

    session.flush()
    record_source_health(session, provider, success=bool(stored))
    return stored


def get_quotes(
    session: Session, tickers: Sequence[str], *, max_age_seconds: int = 120,
    refresh: bool = True,
) -> dict[str, Quote]:
    """Read cached quotes, refreshing any that are stale.

    The cache exists so a page render never blocks on a vendor call, and so the
    vendor is not hit once per ticker per visitor.
    """
    wanted = [t.upper() for t in tickers]
    if not wanted:
        return {}

    rows = {
        q.ticker: q for q in session.execute(
            select(Quote).where(Quote.ticker.in_(wanted))
        ).scalars().all()
    }
    if not refresh:
        return rows

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=max_age_seconds)
    stale = [t for t in wanted if t not in rows or rows[t].retrieved_at < cutoff]
    if stale:
        rows.update(refresh_quotes(session, stale))
    return rows


def get_quote(session: Session, ticker: str, **kwargs: Any) -> Quote | None:
    return get_quotes(session, [ticker], **kwargs).get(ticker.upper())


def record_source_health(
    session: Session, provider: QuoteProvider, *,
    success: bool = False, error: str | None = None,
) -> DataSourceRecord:
    """Keep the data-source registry current for the admin panel."""
    row = session.scalar(select(DataSourceRecord).where(DataSourceRecord.name == provider.name))
    if row is None:
        row = DataSourceRecord(name=provider.name, kind="quotes")
        session.add(row)
    row.kind = "quotes"
    row.enabled = True
    row.is_demo = provider.is_demo
    row.requires_credentials = provider.requires_credentials
    row.credentials_present = provider.is_available()
    row.delayed_minutes = provider.delayed_minutes
    row.notes = provider.status_note()
    stamp = datetime.now(timezone.utc).replace(tzinfo=None)
    if error:
        row.last_error_at = stamp
        row.last_error = error
    if success:
        row.last_success_at = stamp
        row.last_error = None
    session.flush()
    return row


def quote_freshness(quote: Quote | None) -> dict[str, Any]:
    """How a quote should be labelled in the UI. Freshness is never hidden."""
    if quote is None:
        return {
            "label": "No data", "badge": "NO DATA", "tone": "muted",
            "detail": "No quote is available for this instrument.",
            "is_demo": False, "delayed": None,
        }
    if quote.is_demo:
        return {
            "label": DEMO_LABEL, "badge": "DEMO DATA", "tone": "demo",
            "detail": (
                "Generated for demonstration. This is not a real market price and "
                "must not be used for any decision."
            ),
            "is_demo": True, "delayed": None,
        }
    if quote.delayed_minutes and quote.delayed_minutes >= 1440:
        days = quote.delayed_minutes // 1440
        return {
            "label": f"End of day · {days}d old", "badge": "END OF DAY", "tone": "delayed",
            "detail": f"Last stored close, {days} day(s) old. Not a live price.",
            "is_demo": False, "delayed": quote.delayed_minutes,
        }
    if quote.delayed_minutes:
        return {
            "label": f"Delayed {quote.delayed_minutes} minutes", "badge": "DELAYED",
            "tone": "delayed",
            "detail": f"Provider supplies data delayed by {quote.delayed_minutes} minutes.",
            "is_demo": False, "delayed": quote.delayed_minutes,
        }
    return {
        "label": "Real time", "badge": "LIVE", "tone": "live",
        "detail": "Real-time quote from the configured market-data provider.",
        "is_demo": False, "delayed": 0,
    }
