"""CSV file provider — the credential-free path for real data.

You export data from whatever source you trust (your broker, EGX filings, a
terminal subscription, a data vendor) and drop it into ``data/manual/``. This
provider reads it and stamps it with a source you control, so the provenance
chain stays honest.

Expected layout (all optional; missing files simply yield no data):

    data/manual/
    ├── prices/<TICKER>.csv
    ├── fundamentals/<TICKER>.csv
    ├── news.csv
    └── disclosures.csv

Column formats are documented in ``docs/DATA_SOURCES.md``. Header matching is
case-insensitive and tolerant of common aliases.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.core.config import get_settings
from backend.core.data_quality import Confidence
from backend.core.logging_config import get_logger
from backend.data.providers.base import (
    CompanyProfileDTO,
    DisclosureDTO,
    DisclosureProvider,
    FinancialStatementDTO,
    FundamentalDataProvider,
    MarketDataProvider,
    NewsDTO,
    NewsProvider,
    PriceBarDTO,
    ProviderCapabilities,
    QuoteDTO,
)

logger = get_logger(__name__)

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y%m%d")


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:  # ISO with time component
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        d = parse_date(text)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None


def parse_float(value: Any) -> float | None:
    """Parse a number, returning None (never 0.0) when it is absent or unparseable."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("−", "-")
    if text in ("", "-", "--", "n/a", "N/A", "NA", "null", "None", "#N/A"):
        return None
    # Accounting-style negatives: (1,234)
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    multiplier = 1.0
    if text and text[-1] in "KkMmBb":
        multiplier = {"k": 1e3, "m": 1e6, "b": 1e9}[text[-1].lower()]
        text = text[:-1]
    if text.endswith("%"):
        text = text[:-1]
    try:
        result = float(text) * multiplier
    except ValueError:
        return None
    return -result if negative else result


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        (k or "").strip().lower().replace(" ", "_").lstrip("﻿"): v
        for k, v in row.items()
    }


def _pick(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _read_csv(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            yield _normalise_row(raw)


class CsvFileProvider(
    MarketDataProvider, FundamentalDataProvider, NewsProvider, DisclosureProvider
):
    """Reads user-supplied CSV exports. Serves all four data domains."""

    name = "csv"

    def __init__(self, base_dir: Path | str | None = None) -> None:
        settings = get_settings()
        self.base_dir = Path(base_dir) if base_dir else settings.csv_dir

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            domains={"prices", "fundamentals", "news", "disclosures", "profile"},
            requires_credentials=False,
            notes=f"Reads CSV exports from {self.base_dir}",
        )

    def is_available(self) -> bool:
        return self.base_dir.exists()

    def unavailable_reason(self) -> str | None:
        if not self.base_dir.exists():
            return f"CSV data directory not found: {self.base_dir}"
        return None

    def _source_label(self, path: Path) -> str:
        """Source records the actual file, so provenance points at real bytes."""
        try:
            rel = path.relative_to(self.base_dir)
        except ValueError:
            rel = path
        return f"csv:{rel}"

    # -- prices ---------------------------------------------------------------
    def get_price_history(
        self, ticker: str, start: date, end: date, *, symbol_hint: str | None = None
    ) -> list[PriceBarDTO]:
        path = self.base_dir / "prices" / f"{ticker.upper()}.csv"
        if not path.exists():
            return []
        quality = self.quality(confidence=Confidence.HIGH)
        quality = type(quality)(
            source=self._source_label(path),
            retrieved_at=quality.retrieved_at,
            confidence=Confidence.HIGH,
        )

        bars: list[PriceBarDTO] = []
        for row in _read_csv(path):
            bar_date = parse_date(_pick(row, "date", "timestamp", "time", "trade_date"))
            close = parse_float(_pick(row, "close", "close_price", "last", "price"))
            if bar_date is None or close is None:
                continue  # unusable row — skipped, never guessed at
            if start and bar_date < start:
                continue
            if end and bar_date > end:
                continue
            bars.append(
                PriceBarDTO(
                    ticker=ticker.upper(),
                    timestamp=bar_date,
                    open=parse_float(_pick(row, "open", "open_price")),
                    high=parse_float(_pick(row, "high", "high_price")),
                    low=parse_float(_pick(row, "low", "low_price")),
                    close=close,
                    adjusted_close=parse_float(
                        _pick(row, "adjusted_close", "adj_close", "adjclose")
                    )
                    or close,
                    volume=parse_float(_pick(row, "volume", "vol", "traded_volume")),
                    quality=quality,
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        return bars

    def get_quote(self, ticker: str, *, symbol_hint: str | None = None) -> QuoteDTO | None:
        """Derive a quote from the most recent CSV bar, plus 52-week statistics."""
        bars = self.get_price_history(ticker, date(1900, 1, 1), date(2100, 1, 1))
        if not bars:
            return None
        last = bars[-1]
        window = [b for b in bars if (last.timestamp - b.timestamp).days <= 365]
        highs = [b.high or b.close for b in window if (b.high or b.close) is not None]
        lows = [b.low or b.close for b in window if (b.low or b.close) is not None]
        vols = [b.volume for b in window if b.volume is not None]
        return QuoteDTO(
            ticker=ticker.upper(),
            price=last.close,
            open=last.open,
            high=last.high,
            low=last.low,
            previous_close=bars[-2].close if len(bars) > 1 else None,
            volume=last.volume,
            week52_high=max(highs) if highs else None,
            week52_low=min(lows) if lows else None,
            average_volume=(sum(vols) / len(vols)) if vols else None,
            as_of=datetime(
                last.timestamp.year, last.timestamp.month, last.timestamp.day,
                tzinfo=timezone.utc,
            ),
            quality=last.quality,
        )

    # -- fundamentals ---------------------------------------------------------
    def get_financial_statements(
        self, ticker: str, *, limit: int = 20, symbol_hint: str | None = None
    ) -> list[FinancialStatementDTO]:
        path = self.base_dir / "fundamentals" / f"{ticker.upper()}.csv"
        if not path.exists():
            return []
        base_q = self.quality()
        quality = type(base_q)(
            source=self._source_label(path),
            retrieved_at=base_q.retrieved_at,
            confidence=Confidence.HIGH,
        )

        statements: list[FinancialStatementDTO] = []
        for row in _read_csv(path):
            period_end = parse_date(_pick(row, "period_end", "period_end_date", "end_date"))
            period = _pick(row, "period", "fiscal_period")
            if period_end is None and period is None:
                continue
            period_type = str(_pick(row, "period_type", "type") or "FY").upper()[:2].rstrip("Y") or "FY"
            period_type = {"Q": "Q", "H": "H", "F": "FY"}.get(period_type[0], "FY")
            if period_end is None:
                continue
            period_str = str(period) if period else f"{period_end.year}-{period_type}"

            revenue = parse_float(_pick(row, "revenue", "sales", "total_revenue"))
            ocf = parse_float(_pick(row, "operating_cash_flow", "ocf", "cfo"))
            capex = parse_float(_pick(row, "capex", "capital_expenditure"))
            fcf = parse_float(_pick(row, "free_cash_flow", "fcf"))
            # FCF is a CALCULATION when both inputs exist; otherwise it stays None.
            if fcf is None and ocf is not None and capex is not None:
                fcf = ocf - abs(capex)

            statements.append(
                FinancialStatementDTO(
                    ticker=ticker.upper(),
                    period=period_str,
                    period_type=period_type,
                    period_end=period_end,
                    available_from=parse_date(
                        _pick(row, "available_from", "publication_date", "release_date", "filing_date")
                    ),
                    revenue=revenue,
                    gross_profit=parse_float(_pick(row, "gross_profit")),
                    ebitda=parse_float(_pick(row, "ebitda")),
                    operating_income=parse_float(_pick(row, "operating_income", "ebit")),
                    net_income=parse_float(_pick(row, "net_income", "net_profit", "profit")),
                    eps=parse_float(_pick(row, "eps", "earnings_per_share")),
                    cash=parse_float(_pick(row, "cash", "cash_and_equivalents")),
                    total_debt=parse_float(_pick(row, "total_debt", "debt")),
                    total_assets=parse_float(_pick(row, "total_assets")),
                    total_equity=parse_float(_pick(row, "total_equity", "equity", "shareholders_equity")),
                    operating_cash_flow=ocf,
                    capex=capex,
                    free_cash_flow=fcf,
                    interest_expense=parse_float(_pick(row, "interest_expense")),
                    current_assets=parse_float(_pick(row, "current_assets")),
                    current_liabilities=parse_float(_pick(row, "current_liabilities")),
                    dividends_paid=parse_float(_pick(row, "dividends_paid", "dividends")),
                    quality=quality,
                )
            )
        statements.sort(key=lambda s: s.period_end, reverse=True)
        return statements[:limit]

    def get_company_profile(
        self, ticker: str, *, symbol_hint: str | None = None
    ) -> CompanyProfileDTO | None:
        path = self.base_dir / "companies.csv"
        for row in _read_csv(path):
            if str(_pick(row, "ticker", "symbol") or "").strip().upper() != ticker.upper():
                continue
            return CompanyProfileDTO(
                ticker=ticker.upper(),
                name=_pick(row, "name", "company_name"),
                sector=_pick(row, "sector"),
                industry=_pick(row, "industry"),
                currency=str(_pick(row, "currency") or "EGP"),
                shares_outstanding=parse_float(_pick(row, "shares_outstanding", "shares")),
                description=_pick(row, "description"),
                quality=self.quality(),
            )
        return None

    # -- news / disclosures ---------------------------------------------------
    def get_news(
        self, ticker: str | None = None, *, limit: int = 50, since: datetime | None = None
    ) -> list[NewsDTO]:
        path = self.base_dir / "news.csv"
        quality = self.quality(confidence=Confidence.MEDIUM)
        items: list[NewsDTO] = []
        for row in _read_csv(path):
            row_ticker = str(_pick(row, "ticker", "symbol") or "").strip().upper() or None
            if ticker and row_ticker != ticker.upper():
                continue
            title = _pick(row, "title", "headline")
            if not title:
                continue
            published = parse_datetime(_pick(row, "publication_date", "date", "published"))
            if since and published and published < since:
                continue
            items.append(
                NewsDTO(
                    ticker=row_ticker,
                    title=str(title),
                    source=_pick(row, "source", "publisher"),
                    url=_pick(row, "url", "link"),
                    publication_date=published,
                    summary=_pick(row, "summary", "description"),
                    quality=quality,
                )
            )
        items.sort(key=lambda n: n.publication_date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return items[:limit]

    def get_disclosures(
        self, ticker: str | None = None, *, limit: int = 50, since: date | None = None
    ) -> list[DisclosureDTO]:
        path = self.base_dir / "disclosures.csv"
        quality = self.quality(confidence=Confidence.HIGH)
        items: list[DisclosureDTO] = []
        for row in _read_csv(path):
            row_ticker = str(_pick(row, "ticker", "symbol") or "").strip().upper() or None
            if ticker and row_ticker != ticker.upper():
                continue
            title = _pick(row, "title", "subject")
            if not title:
                continue
            d = parse_date(_pick(row, "date", "disclosure_date"))
            if since and d and d < since:
                continue
            items.append(
                DisclosureDTO(
                    ticker=row_ticker,
                    title=str(title),
                    date=d,
                    disclosure_type=_pick(row, "type", "disclosure_type", "category"),
                    url=_pick(row, "url", "link"),
                    summary=_pick(row, "summary", "description"),
                    quality=quality,
                )
            )
        items.sort(key=lambda x: x.date or date.min, reverse=True)
        return items[:limit]
