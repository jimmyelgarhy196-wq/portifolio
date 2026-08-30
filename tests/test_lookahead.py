"""Look-ahead bias prevention.

These are the most important tests in the suite. A backtest that can see the
future produces results that are not merely wrong but actively misleading, and
the failure is silent. Each test here attacks the point-in-time layer directly.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.backtesting.point_in_time import (
    LookAheadError,
    PointInTimeDataView,
    rebalance_dates,
    trading_calendar,
)
from tests.conftest import make_prices, make_statements


class TestPriceCursor:
    def test_never_returns_bars_after_cursor(self, db, company):
        make_prices(db, "TEST", days=200)
        cursor = date.today() - timedelta(days=60)
        view = PointInTimeDataView(session=db, as_of=cursor)
        series = view.price_series("TEST")
        assert series.dates, "expected some history before the cursor"
        assert max(series.dates) <= cursor

    def test_price_query_respects_cursor(self, db, company):
        make_prices(db, "TEST", days=200)
        cursor = date.today() - timedelta(days=90)
        early = PointInTimeDataView(session=db, as_of=cursor).price("TEST")
        late = PointInTimeDataView(session=db, as_of=date.today()).price("TEST")
        assert early is not None and late is not None
        assert early != late, "cursor had no effect on the price returned"

    def test_reading_a_future_date_raises(self, db, company):
        make_prices(db, "TEST", days=100)
        view = PointInTimeDataView(session=db, as_of=date.today() - timedelta(days=30))
        with pytest.raises(LookAheadError):
            view.price("TEST", on=date.today())
        with pytest.raises(LookAheadError):
            view.price_series("TEST", end=date.today())

    def test_cursor_cannot_rewind(self, db):
        view = PointInTimeDataView(session=db, as_of=date(2024, 6, 1))
        with pytest.raises(ValueError):
            view.advance(date(2024, 1, 1))
        assert view.advance(date(2024, 12, 1)).as_of == date(2024, 12, 1)


class TestFundamentalPublicationLag:
    """The crux: a result is invisible until it was actually published."""

    def test_statement_hidden_before_publication_date(self, db, company):
        statements = make_statements(db, "TEST", years=3)
        latest = max(statements, key=lambda s: s.period_end)

        # A cursor after the period ended but before the result was published.
        mid = latest.period_end + timedelta(days=10)
        assert mid < latest.available_from

        visible = PointInTimeDataView(session=db, as_of=mid).financial_periods("TEST")
        assert latest.period not in [p.period for p in visible], (
            "a statement was visible before its publication date — this is "
            "look-ahead bias in the most damaging place"
        )

    def test_statement_appears_on_publication_date(self, db, company):
        statements = make_statements(db, "TEST", years=3)
        latest = max(statements, key=lambda s: s.period_end)
        visible = PointInTimeDataView(
            session=db, as_of=latest.available_from
        ).financial_periods("TEST")
        assert latest.period in [p.period for p in visible]

    def test_statement_without_publication_date_is_never_served(self, db, company):
        """A statement with no known publication date cannot be safely used."""
        from backend.data.models import FinancialStatement

        db.add(FinancialStatement(
            ticker="TEST", period="2024-FY", period_type="FY",
            period_end=date(2024, 12, 31), available_from=None,
            revenue=1000.0, source="test",
        ))
        db.flush()
        visible = PointInTimeDataView(
            session=db, as_of=date(2026, 1, 1)
        ).financial_periods("TEST")
        assert visible == [], (
            "a statement with an unknown publication date was served; without "
            "that date it is impossible to know it was not look-ahead"
        )

    def test_ordering_is_newest_first(self, db, company):
        make_statements(db, "TEST", years=4)
        periods = PointInTimeDataView(
            session=db, as_of=date.today()
        ).financial_periods("TEST")
        assert periods == sorted(periods, key=lambda p: p.period_end, reverse=True)


class TestDeliberateLookAheadAttempt:
    def test_strategy_cannot_reach_past_the_cursor(self, db, company):
        """A strategy that deliberately tries to read ahead gets nothing."""
        bars = make_prices(db, "TEST", days=300)
        # Anchor the cursor on a real trading day; the EGX is shut Fri/Sat, so
        # an arbitrary calendar date may legitimately have no bar at all.
        cursor = bars[len(bars) // 2].timestamp
        view = PointInTimeDataView(session=db, as_of=cursor)

        # Attempt 1: ask for a very long lookback, hoping to overshoot.
        series = view.price_series("TEST", lookback_days=100_000)
        assert max(series.dates) <= cursor

        # Attempt 2: request a specific future bar.
        assert view.bar("TEST", on=cursor) is not None
        with pytest.raises(LookAheadError):
            view.bar("TEST", on=cursor + timedelta(days=1))

        # Attempt 3: news and disclosures are bounded too.
        assert all(
            n.publication_date.date() <= cursor
            for n in view.news("TEST", days=10_000)
            if n.publication_date
        )


class TestTradingCalendar:
    def test_calendar_comes_from_real_bars_only(self, db, company):
        make_prices(db, "TEST", days=120)
        calendar = trading_calendar(db, date.today() - timedelta(days=400), date.today())
        assert calendar
        # No Friday or Saturday: EGX is closed, and the calendar is derived from
        # stored bars rather than assumed, so holidays simply do not appear.
        assert all(d.weekday() not in (4, 5) for d in calendar)
        assert calendar == sorted(calendar)

    def test_rebalance_dates_land_on_trading_days(self, db, company):
        make_prices(db, "TEST", days=400)
        calendar = trading_calendar(db, date.today() - timedelta(days=700), date.today())
        for frequency in ("weekly", "monthly", "quarterly"):
            dates = rebalance_dates(calendar, frequency)
            assert dates
            assert set(dates).issubset(set(calendar)), (
                f"{frequency} rebalance landed on a day the market was closed"
            )

    def test_monthly_gives_one_per_month(self, db, company):
        make_prices(db, "TEST", days=400)
        calendar = trading_calendar(db, date.today() - timedelta(days=700), date.today())
        dates = rebalance_dates(calendar, "monthly")
        months = [(d.year, d.month) for d in dates]
        assert len(months) == len(set(months))

    def test_unknown_frequency_rejected(self):
        with pytest.raises(ValueError):
            rebalance_dates([date(2024, 1, 1)], "fortnightly")


class TestBacktestExecution:
    def test_signal_and_fill_are_on_different_days(self, db, company, benchmark):
        """Filling at the close that generated the signal is look-ahead."""
        from backend.backtesting.engine import BacktestConfig, run_backtest

        make_prices(db, "TEST", days=300)
        make_prices(db, "EGX30", days=300, start_price=1000.0)
        make_statements(db, "TEST", years=4)

        result = run_backtest(db, BacktestConfig(
            strategy="buy_and_hold", index="egx30", rebalance="monthly",
            initial_capital=1_000_000,
        ))
        if result.rebalance_log and result.closed_trades:
            first_decision = date.fromisoformat(result.rebalance_log[0]["date"])
            first_entry = min(t.opened_on for t in result.closed_trades)
            assert first_entry > first_decision, (
                "a position was opened on the same bar that generated the signal"
            )
