"""Point-in-time data access — structural look-ahead prevention.

The backtester cannot see the future because this layer refuses to serve it.

Every query is bounded by ``as_of``:

* **Prices** — no bar dated after ``as_of``, ever.
* **Financial statements** — filtered by ``available_from``, the date a result
  was actually *published*, not the period it covers. A December year-end that
  reported in March is invisible until March.
* **News and disclosures** — filtered by publication date.

Discipline is not the mechanism here; the API is. A strategy cannot read ahead
because there is no method that would return future data, and
``tests/test_lookahead.py`` asserts exactly that.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analytics.fundamental import FinancialPeriod
from backend.analytics.service import PriceSeries
from backend.data.models import (
    Company,
    Disclosure,
    FinancialStatement,
    NewsItem,
    PriceBar,
)


class LookAheadError(RuntimeError):
    """Raised when code attempts to read data beyond the point-in-time cursor."""


@dataclass
class PointInTimeDataView:
    """A read-only window onto the database as it stood on ``as_of``.

    Construct one per simulation step. Every accessor is bounded by the cursor,
    so a strategy holding this object cannot reach past it.
    """

    session: Session
    as_of: date

    def _guard(self, requested: date | None) -> None:
        if requested is not None and requested > self.as_of:
            raise LookAheadError(
                f"Attempted to read data dated {requested.isoformat()}, which is after "
                f"the point-in-time cursor of {self.as_of.isoformat()}. This would be "
                "look-ahead bias."
            )

    # -- prices ---------------------------------------------------------------
    def price_series(
        self, ticker: str, *, lookback_days: int = 800, end: date | None = None
    ) -> PriceSeries:
        self._guard(end)
        end = min(end or self.as_of, self.as_of)
        start = end - timedelta(days=lookback_days)
        rows = self.session.execute(
            select(PriceBar)
            .where(
                PriceBar.ticker == ticker.upper(),
                PriceBar.timestamp >= start,
                PriceBar.timestamp <= end,
            )
            .order_by(PriceBar.timestamp)
        ).scalars().all()

        series = PriceSeries()
        for row in rows:
            series.dates.append(row.timestamp)
            series.opens.append(row.open)
            series.highs.append(row.high)
            series.lows.append(row.low)
            series.closes.append(
                row.adjusted_close if row.adjusted_close is not None else row.close
            )
            series.volumes.append(row.volume)
        if rows:
            series.retrieved_at = max(r.retrieved_at for r in rows)
            series.source = rows[-1].source
        return series

    def price(self, ticker: str, *, on: date | None = None) -> float | None:
        """Last close on or before the cursor. Never a future price."""
        self._guard(on)
        cutoff = min(on or self.as_of, self.as_of)
        bar = self.session.scalar(
            select(PriceBar)
            .where(PriceBar.ticker == ticker.upper(), PriceBar.timestamp <= cutoff)
            .order_by(PriceBar.timestamp.desc())
        )
        if bar is None:
            return None
        return bar.close if bar.close is not None else bar.adjusted_close

    def bar(self, ticker: str, *, on: date | None = None) -> PriceBar | None:
        """The exact bar for a date, if one exists on or before the cursor."""
        self._guard(on)
        cutoff = min(on or self.as_of, self.as_of)
        return self.session.scalar(
            select(PriceBar)
            .where(PriceBar.ticker == ticker.upper(), PriceBar.timestamp == cutoff)
        )

    # -- fundamentals ---------------------------------------------------------
    def financial_periods(
        self, ticker: str, *, period_type: str = "FY", limit: int = 8
    ) -> list[FinancialPeriod]:
        """Statements **published** on or before the cursor.

        This is the crux of look-ahead prevention for fundamentals. Filtering on
        ``period_end`` would hand the simulation a full-year result on 31
        December, months before anyone could have read it.
        """
        rows = self.session.execute(
            select(FinancialStatement)
            .where(
                FinancialStatement.ticker == ticker.upper(),
                FinancialStatement.period_type == period_type,
                FinancialStatement.available_from.isnot(None),
                FinancialStatement.available_from <= self.as_of,
            )
            .order_by(FinancialStatement.period_end.desc())
            .limit(limit)
        ).scalars().all()
        return [FinancialPeriod.from_model(row) for row in rows]

    # -- events ---------------------------------------------------------------
    def disclosures(self, ticker: str, *, days: int = 180) -> list[Disclosure]:
        since = self.as_of - timedelta(days=days)
        return list(self.session.execute(
            select(Disclosure)
            .where(
                Disclosure.ticker == ticker.upper(),
                Disclosure.date >= since,
                Disclosure.date <= self.as_of,
            )
            .order_by(Disclosure.date.desc())
        ).scalars().all())

    def news(self, ticker: str, *, days: int = 30) -> list[NewsItem]:
        cutoff = datetime(self.as_of.year, self.as_of.month, self.as_of.day)
        since = cutoff - timedelta(days=days)
        return list(self.session.execute(
            select(NewsItem)
            .where(
                NewsItem.ticker == ticker.upper(),
                NewsItem.publication_date >= since,
                NewsItem.publication_date <= cutoff,
            )
            .order_by(NewsItem.publication_date.desc())
        ).scalars().all())

    # -- universe -------------------------------------------------------------
    def companies(self, tickers: Sequence[str] | None = None) -> list[Company]:
        stmt = select(Company).where(Company.status != "INDEX")
        if tickers:
            stmt = stmt.where(Company.ticker.in_([t.upper() for t in tickers]))
        return list(self.session.execute(stmt.order_by(Company.ticker)).scalars().all())

    def tradeable(self, ticker: str) -> bool:
        """A name is tradeable only if it has a price on or before the cursor."""
        return self.price(ticker) is not None

    def advance(self, to: date) -> "PointInTimeDataView":
        """Move the cursor forward. It can never move backwards."""
        if to < self.as_of:
            raise ValueError(
                f"Cannot rewind the point-in-time cursor from {self.as_of} to {to}."
            )
        return PointInTimeDataView(session=self.session, as_of=to)


def trading_calendar(
    session: Session, start: date, end: date, *, reference_ticker: str | None = None
) -> list[date]:
    """Dates on which the market actually traded, from stored bars.

    Derived from data rather than assumed, so market holidays are handled by
    simply not existing — no synthetic dates are ever created.
    """
    stmt = select(PriceBar.timestamp).where(
        PriceBar.timestamp >= start, PriceBar.timestamp <= end
    )
    if reference_ticker:
        stmt = stmt.where(PriceBar.ticker == reference_ticker.upper())
    dates = session.execute(stmt.distinct().order_by(PriceBar.timestamp)).scalars().all()
    return list(dates)


def rebalance_dates(
    calendar: Sequence[date], frequency: str = "monthly"
) -> list[date]:
    """Select rebalance dates from a trading calendar.

    Only real trading days are returned, so a rebalance never lands on a day
    the market was closed.
    """
    if not calendar:
        return []
    frequency = frequency.lower()
    if frequency in ("daily", "d"):
        return list(calendar)

    out: list[date] = []
    if frequency in ("weekly", "w"):
        current_key = None
        for day in calendar:
            key = day.isocalendar()[:2]
            if key != current_key:
                out.append(day)
                current_key = key
    elif frequency in ("monthly", "m"):
        current_key = None
        for day in calendar:
            key = (day.year, day.month)
            if key != current_key:
                out.append(day)
                current_key = key
    elif frequency in ("quarterly", "q"):
        current_key = None
        for day in calendar:
            key = (day.year, (day.month - 1) // 3)
            if key != current_key:
                out.append(day)
                current_key = key
    elif frequency in ("yearly", "annual", "y"):
        current_key = None
        for day in calendar:
            if day.year != current_key:
                out.append(day)
                current_key = day.year
    else:
        raise ValueError(f"Unknown rebalance frequency {frequency!r}")
    return out
