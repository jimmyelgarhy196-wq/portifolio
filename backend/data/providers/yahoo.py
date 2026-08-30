"""Yahoo Finance market-data provider.

EGX equities are quoted on Yahoo with a ``.CA`` suffix (``COMI.CA``,
``HRHO.CA``); the EGX30 index is ``^CCSI``. No credentials are required.

Coverage caveat, stated plainly: Yahoo carries EGX **prices** reliably but its
EGX **fundamentals** coverage is sparse to non-existent. This provider therefore
implements market data only. Fundamentals default to the CSV provider, where you
supply audited figures yourself.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from backend.core.config import get_settings
from backend.core.data_quality import Confidence
from backend.core.logging_config import get_logger
from backend.data.providers.base import (
    MarketDataProvider,
    PriceBarDTO,
    ProviderCapabilities,
    ProviderUnavailable,
    QuoteDTO,
)
from backend.data.providers.http_client import HttpFetcher

logger = get_logger(__name__)


def to_yahoo_symbol(ticker: str, symbol_hint: str | None = None) -> str:
    """Map an EGX ticker to its Yahoo symbol."""
    if symbol_hint:
        return symbol_hint
    t = ticker.strip().upper()
    if t.startswith("^") or "." in t:
        return t
    if t in {"EGX30", "CASE30"}:
        return "^CCSI"
    return f"{t}.CA"


class YahooFinanceProvider(MarketDataProvider):
    """Reads the public Yahoo Finance chart endpoint."""

    name = "yahoo"

    def __init__(self, fetcher: HttpFetcher | None = None) -> None:
        settings = get_settings()
        self._fetcher = fetcher or HttpFetcher(base_url=settings.yahoo_base_url)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            domains={"prices", "quotes"},
            requires_credentials=False,
            notes=(
                "EGX symbols use the .CA suffix; EGX30 index is ^CCSI. "
                "Prices only — Yahoo does not reliably carry EGX fundamentals."
            ),
        )

    # -- prices ---------------------------------------------------------------
    def get_price_history(
        self, ticker: str, start: date, end: date, *, symbol_hint: str | None = None
    ) -> list[PriceBarDTO]:
        symbol = to_yahoo_symbol(ticker, symbol_hint)
        params = {
            "period1": int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()),
            "period2": int(
                datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()
            ),
            "interval": "1d",
            "events": "div,split",
            "includeAdjustedClose": "true",
        }
        payload = self._fetcher.get_json(f"/v8/finance/chart/{symbol}", params=params)
        return self._parse_chart(ticker, payload)

    def _parse_chart(self, ticker: str, payload: Any) -> list[PriceBarDTO]:
        chart = (payload or {}).get("chart") or {}
        if chart.get("error"):
            raise ProviderUnavailable(f"Yahoo error for {ticker}: {chart['error']}")
        results = chart.get("result") or []
        if not results:
            return []

        result = results[0]
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        adjclose_block = ((result.get("indicators") or {}).get("adjclose") or [{}])[0]
        adjclose = adjclose_block.get("adjclose") or []

        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        quality = self.quality(confidence=Confidence.HIGH)
        bars: list[PriceBarDTO] = []

        for i, ts in enumerate(timestamps):
            close = _at(closes, i)
            # A bar with no close carries no information — skip rather than
            # interpolate, which would be fabrication.
            if close is None:
                continue
            bars.append(
                PriceBarDTO(
                    ticker=ticker,
                    timestamp=datetime.fromtimestamp(ts, tz=timezone.utc).date(),
                    open=_at(opens, i),
                    high=_at(highs, i),
                    low=_at(lows, i),
                    close=close,
                    adjusted_close=_at(adjclose, i, default=close),
                    volume=_at(volumes, i),
                    quality=quality,
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        return bars

    # -- quote ----------------------------------------------------------------
    def get_quote(self, ticker: str, *, symbol_hint: str | None = None) -> QuoteDTO | None:
        symbol = to_yahoo_symbol(ticker, symbol_hint)
        payload = self._fetcher.get_json(
            f"/v8/finance/chart/{symbol}", params={"range": "1d", "interval": "1d"}
        )
        results = ((payload or {}).get("chart") or {}).get("result") or []
        if not results:
            return None
        meta = results[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        return QuoteDTO(
            ticker=ticker,
            price=price,
            open=meta.get("regularMarketOpen") or meta.get("chartPreviousClose"),
            high=meta.get("regularMarketDayHigh"),
            low=meta.get("regularMarketDayLow"),
            previous_close=meta.get("chartPreviousClose") or meta.get("previousClose"),
            volume=meta.get("regularMarketVolume"),
            week52_high=meta.get("fiftyTwoWeekHigh"),
            week52_low=meta.get("fiftyTwoWeekLow"),
            currency=meta.get("currency") or "EGP",
            as_of=datetime.now(timezone.utc),
            quality=self.quality(confidence=Confidence.HIGH),
        )

    def close(self) -> None:
        self._fetcher.close()


def _at(seq: list[Any], index: int, default: Any = None) -> Any:
    """Read ``seq[index]`` tolerating short arrays and Yahoo's ``null`` gaps."""
    if index < len(seq):
        value = seq[index]
        if value is not None:
            return value
    return default
