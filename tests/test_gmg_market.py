"""Quote providers, freshness labelling, market aggregation and search.

These tests exist because the platform's central promise is that it never
presents a fabricated number as a real one. Each test below pins one part of
that promise.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta

import pytest

from backend.data import models, saas_models
from backend.market import quotes as Q
from backend.market.overview import (
    INDEX_DEFINITIONS,
    breadth,
    index_snapshot,
    market_overview,
    movers,
    search_companies,
)
from backend.market.status import MarketStatus, market_state


@pytest.fixture
def universe(db):
    rows = [
        ("COMI", "Commercial International Bank", "Banks", True, 3.0e9),
        ("HRHO", "EFG Holding", "Financial Services", True, 1.2e9),
        ("SWDY", "Elsewedy Electric", "Industrials", True, 2.1e9),
        ("ORWE", "Oriental Weavers", "Consumer", False, 4.5e8),
        ("JUFO", "Juhayna Food", "Consumer", False, 9.4e8),
    ]
    for ticker, name, sector, e30, shares in rows:
        db.add(models.Company(
            ticker=ticker, name=name, sector=sector, exchange="EGX", currency="EGP",
            status="ACTIVE", in_egx30=e30, in_egx70=not e30, in_egx100=True,
            shares_outstanding=shares,
        ))
    db.flush()
    return rows


class TestMarketSession:
    def test_egx_trades_sunday_to_thursday(self):
        # Sunday 11:00 Cairo == 09:00 UTC
        sunday = datetime(2026, 8, 30, 9, 0)
        assert market_state(sunday).status is MarketStatus.OPEN

    def test_closed_before_the_open(self):
        state = market_state(datetime(2026, 8, 30, 6, 30))   # 08:30 Cairo
        assert state.status is MarketStatus.PRE_OPEN
        assert "Opens in" in state.note

    def test_closed_on_friday(self):
        state = market_state(datetime(2026, 8, 28, 9, 0))
        assert state.status is MarketStatus.CLOSED
        assert "Sunday to Thursday" in state.note

    def test_open_state_is_the_only_live_one(self):
        assert MarketStatus.OPEN.is_live
        assert not MarketStatus.CLOSED.is_live
        assert not MarketStatus.PRE_OPEN.is_live


class TestQuoteProviders:
    def test_demo_provider_is_deterministic_within_a_day(self, db, universe):
        first = Q.DemoQuoteProvider(db).get_quotes(["COMI"])["COMI"]
        second = Q.DemoQuoteProvider(db).get_quotes(["COMI"])["COMI"]
        assert first.price == second.price

    def test_every_demo_quote_is_stamped(self, db, universe):
        for quote in Q.DemoQuoteProvider(db).get_quotes(["COMI", "HRHO"]).values():
            assert quote.is_demo is True
            assert quote.source.startswith("DEMO:")

    def test_demo_ohlc_is_internally_consistent(self, db, universe):
        for quote in Q.DemoQuoteProvider(db).get_quotes(["COMI", "HRHO", "SWDY"]).values():
            assert quote.day_low <= quote.price <= quote.day_high
            assert quote.day_low <= quote.open <= quote.day_high

    def test_licensed_provider_refuses_rather_than_falling_back(self, db, monkeypatch):
        """The one failure mode this architecture exists to prevent: a silent
        downgrade that puts generated prices behind a live label."""
        from backend.core.config import get_settings

        monkeypatch.setenv("EGX_MARKET_DATA_API_KEY", "present-but-unimplemented")
        get_settings.cache_clear()
        try:
            provider = Q.LicensedQuoteProvider()
            assert provider.is_available()
            with pytest.raises(NotImplementedError):
                provider.get_quotes(["COMI"])
        finally:
            monkeypatch.delenv("EGX_MARKET_DATA_API_KEY", raising=False)
            get_settings.cache_clear()

    def test_licensed_provider_serves_nothing_without_credentials(self, db):
        provider = Q.LicensedQuoteProvider()
        assert provider.get_quotes(["COMI"]) == {}
        assert "credentials" in (provider.unavailable_reason() or "").lower()

    def test_stored_provider_never_invents_a_missing_ticker(self, db, universe):
        assert Q.StoredPriceQuoteProvider(db).get_quotes(["NOSUCH"]) == {}

    def test_stored_provider_reports_the_true_age_of_its_bar(self, db, universe):
        db.add(models.PriceBar(
            ticker="COMI", timestamp=date.today() - timedelta(days=3),
            open=60, high=62, low=59, close=61, volume=1_000_000, source="EGX:official",
        ))
        db.flush()
        quote = Q.StoredPriceQuoteProvider(db).get_quotes(["COMI"])["COMI"]
        assert quote.delayed_minutes == 3 * 24 * 60
        assert quote.is_demo is False

    def test_synthetic_bars_are_flagged_as_demo(self, db, universe):
        db.add(models.PriceBar(
            ticker="COMI", timestamp=date.today(), close=61.0, volume=5,
            source="SYNTHETIC_DEMO:seed",
        ))
        db.flush()
        assert Q.StoredPriceQuoteProvider(db).get_quotes(["COMI"])["COMI"].is_demo

    def test_provider_selection_prefers_real_data_over_demo(self, db, universe):
        assert Q.build_quote_provider(db).is_demo is True   # nothing stored yet
        db.add(models.PriceBar(
            ticker="COMI", timestamp=date.today(), close=61.0, volume=5,
            source="EGX:official",
        ))
        db.flush()
        assert Q.build_quote_provider(db).name == "stored"


class TestFreshnessLabels:
    def _quote(self, **kwargs):
        defaults = dict(ticker="X", price=10.0, is_demo=False, delayed_minutes=0)
        defaults.update(kwargs)
        return saas_models.Quote(**defaults)

    def test_no_quote_says_so(self):
        assert Q.quote_freshness(None)["badge"] == "NO DATA"

    def test_demo_beats_every_other_label(self):
        result = Q.quote_freshness(self._quote(is_demo=True, delayed_minutes=0))
        assert result["badge"] == "DEMO DATA"
        assert "not a real market price" in result["detail"]

    def test_stale_bar_reads_end_of_day(self):
        assert Q.quote_freshness(self._quote(delayed_minutes=2880))["badge"] == "END OF DAY"

    def test_delayed_quote_names_its_delay(self):
        result = Q.quote_freshness(self._quote(delayed_minutes=15))
        assert result["badge"] == "DELAYED"
        assert "15" in result["label"]

    def test_only_a_real_undelayed_quote_is_live(self):
        assert Q.quote_freshness(self._quote())["badge"] == "LIVE"


class TestAggregation:
    def test_index_level_is_never_invented(self, db, universe):
        overview = market_overview(db)
        for snapshot in overview.indices:
            assert snapshot.is_official is False
            assert snapshot.level is None, "no official feed exists, so no level may be shown"
            assert "not the official index level" in snapshot.note.lower()

    def test_index_without_quotes_reports_unavailable(self, db, universe):
        snapshot = index_snapshot(db, INDEX_DEFINITIONS[0], quotes={})
        assert snapshot.change_pct is None
        assert "N/A" in snapshot.note

    def test_demo_inputs_contaminate_every_aggregate(self, db, universe):
        overview = market_overview(db)
        assert overview.is_demo
        assert overview.breadth.is_demo
        assert all(i.is_demo for i in overview.indices if i.covered)
        assert overview.quote_badge == "DEMO DATA"
        assert "DEMO DATA" in overview.data_note

    def test_breadth_reconciles_with_coverage(self, db, universe):
        overview = market_overview(db)
        depth = overview.breadth
        assert depth.advancers + depth.decliners + depth.unchanged == depth.covered
        assert depth.covered <= depth.universe

    def test_coverage_is_reported_not_hidden(self, db, universe):
        overview = market_overview(db)
        assert overview.universe_size == 5
        assert overview.covered <= overview.universe_size

    def test_movers_exclude_rows_without_a_move(self, db, universe):
        companies = list(db.query(models.Company).all())
        quotes = Q.refresh_quotes(db, [c.ticker for c in companies])
        # A quote with no previous close has no change and must not be ranked.
        quotes["ORWE"].change_pct = None
        gainers, losers, active = movers(companies, quotes)
        assert "ORWE" not in {row.ticker for row in gainers + losers}

    def test_empty_universe_produces_no_data_not_zeros(self, db):
        overview = market_overview(db)
        assert overview.quote_badge == "NO DATA"
        assert overview.breadth.total_volume is None
        assert overview.breadth.advancers == 0


class TestSearch:
    def test_exact_ticker_ranks_first(self, db, universe):
        hits = search_companies(db, "COMI")
        assert hits[0].ticker == "COMI"

    def test_name_search_works(self, db, universe):
        assert "HRHO" in {h.ticker for h in search_companies(db, "EFG")}

    def test_sector_search_works(self, db, universe):
        assert {"ORWE", "JUFO"} <= {h.ticker for h in search_companies(db, "Consumer")}

    def test_no_match_returns_nothing_rather_than_a_guess(self, db, universe):
        assert search_companies(db, "zzzzz") == []

    def test_empty_query_returns_nothing(self, db, universe):
        assert search_companies(db, "  ") == []

    def test_index_membership_travels_with_the_hit(self, db, universe):
        hit = search_companies(db, "COMI")[0]
        assert "EGX30" in hit.indices
