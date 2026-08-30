"""SYNTHETIC DEMO PROVIDER — FICTIONAL DATA. NOT A DATA SOURCE.

=============================================================================
READ THIS BEFORE USING ANYTHING THIS MODULE PRODUCES
=============================================================================
Every number this provider returns is INVENTED by a random-number generator.
No price, no financial statement, and no company figure here corresponds to any
real security, on the EGX or anywhere else.

It exists for exactly two reasons:
  1. So the terminal can be demonstrated without a data subscription.
  2. So the test suite can exercise the full pipeline deterministically offline.

Four independent guards keep it out of real analysis:
  * Disabled unless ``EGX_ALLOW_SYNTHETIC_DATA=true``; the constructor raises
    otherwise.
  * Every record is stamped ``source="SYNTHETIC_DEMO:synthetic"`` in the
    database, so provenance always exposes it.
  * Confidence is fixed at ``UNVERIFIED``, the lowest tier.
  * The UI renders a permanent red banner and reports refuse to generate
    without an explicit acknowledgement.

The generator is seeded per ticker, so runs are reproducible.
=============================================================================
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

from backend.core.config import get_settings
from backend.core.data_quality import Confidence
from backend.core.logging_config import get_logger
from backend.data.providers.base import (
    FinancialStatementDTO,
    FundamentalDataProvider,
    MarketDataProvider,
    PriceBarDTO,
    ProviderCapabilities,
    ProviderUnavailable,
    QuoteDTO,
)

logger = get_logger(__name__)

SYNTHETIC_WARNING = (
    "SYNTHETIC DEMO DATA — these figures are randomly generated and correspond "
    "to no real security. Do not act on them."
)


class SyntheticProviderDisabled(ProviderUnavailable):
    """Raised when synthetic data is requested but not explicitly enabled."""


class SyntheticProvider(MarketDataProvider, FundamentalDataProvider):
    """Generates fictional but internally consistent market and financial data."""

    name = "synthetic"
    is_synthetic = True

    def __init__(self, *, force: bool = False, seed: int = 20240101) -> None:
        settings = get_settings()
        if not settings.allow_synthetic_data and not force:
            raise SyntheticProviderDisabled(
                "Synthetic data is disabled. Set EGX_ALLOW_SYNTHETIC_DATA=true to "
                "enable it for offline demonstration only. It produces FICTIONAL data."
            )
        self._seed = seed
        logger.warning(SYNTHETIC_WARNING)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            domains={"prices", "quotes", "fundamentals"},
            is_synthetic=True,
            notes=SYNTHETIC_WARNING,
        )

    def _rng(self, ticker: str, salt: str = "") -> random.Random:
        return random.Random(f"{self._seed}:{ticker.upper()}:{salt}")

    def _profile(self, ticker: str) -> dict[str, float]:
        """Stable per-ticker character: price level, drift, vol, quality tilt."""
        rng = self._rng(ticker, "profile")
        return {
            "start_price": rng.uniform(4.0, 120.0),
            "annual_drift": rng.uniform(-0.18, 0.34),
            "annual_vol": rng.uniform(0.18, 0.62),
            "base_volume": rng.uniform(50_000, 6_000_000),
            "margin": rng.uniform(0.04, 0.34),
            "growth": rng.uniform(-0.10, 0.40),
            "leverage": rng.uniform(0.05, 1.9),
            "shares": rng.uniform(50e6, 3.2e9),
        }

    # -- prices ---------------------------------------------------------------
    def get_price_history(
        self, ticker: str, start: date, end: date, *, symbol_hint: str | None = None
    ) -> list[PriceBarDTO]:
        profile = self._profile(ticker)
        rng = self._rng(ticker, "prices")
        quality = self.quality(confidence=Confidence.UNVERIFIED, note=SYNTHETIC_WARNING)

        daily_drift = profile["annual_drift"] / 252.0
        daily_vol = profile["annual_vol"] / math.sqrt(252.0)
        price = profile["start_price"]
        bars: list[PriceBarDTO] = []
        current = start

        while current <= end:
            # Skip Friday/Saturday — the EGX trading week is Sunday-Thursday.
            if current.weekday() in (4, 5):
                current += timedelta(days=1)
                continue
            shock = rng.gauss(daily_drift, daily_vol)
            close = max(0.35, price * (1.0 + shock))
            intraday = abs(rng.gauss(0, daily_vol * 0.6))
            high = close * (1.0 + intraday)
            low = close * (1.0 - intraday)
            open_price = low + (high - low) * rng.random()
            volume = max(1000.0, profile["base_volume"] * rng.lognormvariate(0, 0.45))

            bars.append(
                PriceBarDTO(
                    ticker=ticker.upper(),
                    timestamp=current,
                    open=round(open_price, 3),
                    high=round(max(high, open_price, close), 3),
                    low=round(min(low, open_price, close), 3),
                    close=round(close, 3),
                    adjusted_close=round(close, 3),
                    volume=round(volume),
                    quality=quality,
                )
            )
            price = close
            current += timedelta(days=1)
        return bars

    def get_quote(self, ticker: str, *, symbol_hint: str | None = None) -> QuoteDTO | None:
        today = date.today()
        bars = self.get_price_history(ticker, today - timedelta(days=400), today)
        if not bars:
            return None
        last = bars[-1]
        highs = [b.high for b in bars if b.high]
        lows = [b.low for b in bars if b.low]
        vols = [b.volume for b in bars if b.volume]
        profile = self._profile(ticker)
        return QuoteDTO(
            ticker=ticker.upper(),
            price=last.close,
            open=last.open,
            high=last.high,
            low=last.low,
            previous_close=bars[-2].close if len(bars) > 1 else None,
            volume=last.volume,
            market_cap=(last.close or 0) * profile["shares"],
            week52_high=max(highs) if highs else None,
            week52_low=min(lows) if lows else None,
            average_volume=sum(vols) / len(vols) if vols else None,
            shares_outstanding=profile["shares"],
            as_of=datetime.now(timezone.utc),
            quality=self.quality(confidence=Confidence.UNVERIFIED, note=SYNTHETIC_WARNING),
        )

    # -- fundamentals ---------------------------------------------------------
    def get_financial_statements(
        self, ticker: str, *, limit: int = 20, symbol_hint: str | None = None
    ) -> list[FinancialStatementDTO]:
        profile = self._profile(ticker)
        rng = self._rng(ticker, "fundamentals")
        quality = self.quality(confidence=Confidence.UNVERIFIED, note=SYNTHETIC_WARNING)

        this_year = date.today().year
        years = min(limit, 6)
        revenue = rng.uniform(500e6, 40e9)
        statements: list[FinancialStatementDTO] = []

        # Build oldest → newest so growth compounds forward sensibly.
        for offset in range(years - 1, -1, -1):
            year = this_year - 1 - offset
            revenue *= 1.0 + profile["growth"] + rng.gauss(0, 0.06)
            revenue = max(revenue, 10e6)

            gross = revenue * min(0.85, profile["margin"] + rng.uniform(0.10, 0.28))
            ebitda = revenue * max(0.02, profile["margin"] + rng.gauss(0, 0.03))
            operating = ebitda * rng.uniform(0.62, 0.90)
            interest = operating * rng.uniform(0.02, 0.22)
            net = (operating - interest) * rng.uniform(0.62, 0.86)
            equity = revenue * rng.uniform(0.55, 1.7)
            debt = equity * profile["leverage"]
            assets = equity + debt + revenue * rng.uniform(0.18, 0.65)
            ocf = net * rng.uniform(0.75, 1.45)
            capex = revenue * rng.uniform(0.02, 0.11)

            period_end = date(year, 12, 31)
            statements.append(
                FinancialStatementDTO(
                    ticker=ticker.upper(),
                    period=f"{year}-FY",
                    period_type="FY",
                    period_end=period_end,
                    # Realistic EGX reporting lag: results land ~11 weeks after
                    # year end. Backtests rely on this to avoid look-ahead.
                    available_from=period_end + timedelta(days=rng.randint(60, 110)),
                    revenue=round(revenue, 2),
                    gross_profit=round(gross, 2),
                    ebitda=round(ebitda, 2),
                    operating_income=round(operating, 2),
                    net_income=round(net, 2),
                    eps=round(net / profile["shares"], 4),
                    cash=round(assets * rng.uniform(0.03, 0.18), 2),
                    total_debt=round(debt, 2),
                    total_assets=round(assets, 2),
                    total_equity=round(equity, 2),
                    operating_cash_flow=round(ocf, 2),
                    capex=round(capex, 2),
                    free_cash_flow=round(ocf - capex, 2),
                    interest_expense=round(interest, 2),
                    current_assets=round(assets * rng.uniform(0.22, 0.52), 2),
                    current_liabilities=round(assets * rng.uniform(0.12, 0.38), 2),
                    dividends_paid=round(net * rng.uniform(0.0, 0.55), 2),
                    quality=quality,
                )
            )
        statements.reverse()  # newest first, matching the interface contract
        return statements[:limit]
