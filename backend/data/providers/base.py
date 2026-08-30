"""Data-provider interfaces.

Four domains, four interfaces. Implementations are registered in
``backend/data/providers/registry.py`` and selected by configuration, so the
source of any dataset can be swapped without touching a caller.

The contract every implementation must honour
---------------------------------------------
1. Return data, or raise :class:`ProviderUnavailable` / return empty. **Never
   substitute a placeholder value for missing data.**
2. Stamp every record with a :class:`~backend.core.data_quality.DataQuality`
   carrying the real source name and retrieval time.
3. Be idempotent: fetching the same range twice yields the same records, so the
   ingestion layer can safely de-duplicate.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from backend.core.data_quality import Confidence, DataQuality


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ProviderError(Exception):
    """Base for all provider failures."""


class ProviderUnavailable(ProviderError):
    """Provider cannot serve the request (network down, no credentials, blocked)."""


class ProviderRateLimited(ProviderError):
    """Provider signalled a rate limit. Ingestion backs off and retries."""


class SymbolNotSupported(ProviderError):
    """Provider has no coverage for this symbol. Not an error condition."""


# ---------------------------------------------------------------------------
# Transport objects
# ---------------------------------------------------------------------------
@dataclass
class PriceBarDTO:
    ticker: str
    timestamp: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    adjusted_close: float | None = None
    volume: float | None = None
    quality: DataQuality | None = None


@dataclass
class QuoteDTO:
    """A current-price snapshot with the summary statistics a terminal shows."""

    ticker: str
    price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    volume: float | None = None
    market_cap: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    average_volume: float | None = None
    shares_outstanding: float | None = None
    currency: str = "EGP"
    as_of: datetime | None = None
    quality: DataQuality | None = None


@dataclass
class FinancialStatementDTO:
    ticker: str
    period: str
    period_type: str          # Q | H | FY
    period_end: date
    #: Publication date. Critical for look-ahead-free backtesting.
    available_from: date | None = None
    revenue: float | None = None
    gross_profit: float | None = None
    ebitda: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    eps: float | None = None
    cash: float | None = None
    total_debt: float | None = None
    total_assets: float | None = None
    total_equity: float | None = None
    operating_cash_flow: float | None = None
    capex: float | None = None
    free_cash_flow: float | None = None
    interest_expense: float | None = None
    current_assets: float | None = None
    current_liabilities: float | None = None
    dividends_paid: float | None = None
    quality: DataQuality | None = None


@dataclass
class NewsDTO:
    ticker: str | None
    title: str
    source: str | None = None
    url: str | None = None
    publication_date: datetime | None = None
    summary: str | None = None
    quality: DataQuality | None = None


@dataclass
class DisclosureDTO:
    ticker: str | None
    title: str
    date: date | None = None
    disclosure_type: str | None = None
    url: str | None = None
    summary: str | None = None
    quality: DataQuality | None = None


@dataclass
class CompanyProfileDTO:
    ticker: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    currency: str = "EGP"
    shares_outstanding: float | None = None
    description: str | None = None
    quality: DataQuality | None = None


@dataclass
class ProviderCapabilities:
    """Declares what a provider can actually serve, so callers do not guess."""

    name: str
    domains: set[str] = field(default_factory=set)  # prices|quotes|fundamentals|news|disclosures|profile
    requires_credentials: bool = False
    is_synthetic: bool = False
    notes: str = ""


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------
class BaseProvider(abc.ABC):
    """Common behaviour: naming, provenance stamping, availability reporting."""

    name: str = "base"
    is_synthetic: bool = False
    requires_credentials: bool = False

    @abc.abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        ...

    def is_available(self) -> bool:
        """Whether this provider can be used right now (credentials present, etc.)."""
        return True

    def unavailable_reason(self) -> str | None:
        return None

    def quality(
        self,
        *,
        data_period: str | None = None,
        confidence: Confidence = Confidence.HIGH,
        note: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> DataQuality:
        return DataQuality(
            source=self.source_name,
            retrieved_at=retrieved_at or datetime.now(timezone.utc),
            data_period=data_period,
            confidence=confidence,
            note=note,
        )

    @property
    def source_name(self) -> str:
        """Stamped onto every record. Synthetic providers are unmistakable."""
        return f"SYNTHETIC_DEMO:{self.name}" if self.is_synthetic else self.name

    def close(self) -> None:  # pragma: no cover - default no-op
        return None


# ---------------------------------------------------------------------------
# Domain interfaces
# ---------------------------------------------------------------------------
class MarketDataProvider(BaseProvider):
    """Prices, quotes and market statistics."""

    @abc.abstractmethod
    def get_price_history(
        self, ticker: str, start: date, end: date, *, symbol_hint: str | None = None
    ) -> list[PriceBarDTO]:
        """Daily bars in ``[start, end]``, ascending. Empty list when uncovered."""

    def get_quote(self, ticker: str, *, symbol_hint: str | None = None) -> QuoteDTO | None:
        """Current snapshot. ``None`` when the provider cannot serve one."""
        return None


class FundamentalDataProvider(BaseProvider):
    """Financial statements and company profiles."""

    @abc.abstractmethod
    def get_financial_statements(
        self, ticker: str, *, limit: int = 20, symbol_hint: str | None = None
    ) -> list[FinancialStatementDTO]:
        """Most recent *limit* statements, newest first."""

    def get_company_profile(
        self, ticker: str, *, symbol_hint: str | None = None
    ) -> CompanyProfileDTO | None:
        return None


class NewsProvider(BaseProvider):
    @abc.abstractmethod
    def get_news(
        self, ticker: str | None = None, *, limit: int = 50, since: datetime | None = None
    ) -> list[NewsDTO]:
        ...


class DisclosureProvider(BaseProvider):
    @abc.abstractmethod
    def get_disclosures(
        self, ticker: str | None = None, *, limit: int = 50, since: date | None = None
    ) -> list[DisclosureDTO]:
        ...


# ---------------------------------------------------------------------------
# Null provider — the honest default
# ---------------------------------------------------------------------------
class NullProvider(
    MarketDataProvider, FundamentalDataProvider, NewsProvider, DisclosureProvider
):
    """Serves nothing, and says so.

    This is what the system uses when no provider is configured. It returns
    empty results rather than fabricating data, which is the entire point.
    """

    name = "null"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            domains=set(),
            notes="No data source configured. All datasets report UNAVAILABLE.",
        )

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str | None:
        return "No data provider configured for this dataset."

    def get_price_history(self, ticker, start, end, *, symbol_hint=None):
        return []

    def get_quote(self, ticker, *, symbol_hint=None):
        return None

    def get_financial_statements(self, ticker, *, limit=20, symbol_hint=None):
        return []

    def get_news(self, ticker=None, *, limit=50, since=None):
        return []

    def get_disclosures(self, ticker=None, *, limit=50, since=None):
        return []
