"""Data ingestion: duplicates, gaps, holidays, bad payloads, provider failures."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.data.ingestion import (
    compute_valuation_snapshot,
    ingest_disclosures,
    ingest_fundamentals,
    ingest_news,
    ingest_prices,
)
from backend.data.models import Company, DataQualityLog, PriceBar
from backend.data.providers.base import (
    NullProvider,
    PriceBarDTO,
    ProviderUnavailable,
    MarketDataProvider,
    ProviderCapabilities,
)
from backend.data.providers.csv_provider import CsvFileProvider, parse_date, parse_float
from backend.data.providers.egx_disclosure import classify_disclosure, url_hash
from backend.data.providers.registry import ProviderChain
from backend.data.providers.rss_news import score_sentiment
from backend.data.providers.synthetic import SyntheticProvider, SyntheticProviderDisabled
from backend.data.providers.yahoo import YahooFinanceProvider, to_yahoo_symbol


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class TestNullProvider:
    def test_returns_nothing_rather_than_fabricating(self):
        provider = NullProvider()
        assert provider.get_price_history("X", date.today(), date.today()) == []
        assert provider.get_quote("X") is None
        assert provider.get_financial_statements("X") == []
        assert provider.get_news() == []
        assert "No data" in provider.unavailable_reason()


class TestYahooProvider:
    def test_symbol_mapping(self):
        assert to_yahoo_symbol("COMI") == "COMI.CA"
        assert to_yahoo_symbol("EGX30") == "^CCSI"
        assert to_yahoo_symbol("HRHO", "OVERRIDE") == "OVERRIDE"

    def test_parses_chart_payload(self):
        provider = YahooFinanceProvider()
        payload = {"chart": {"error": None, "result": [{
            "meta": {"currency": "EGP"},
            "timestamp": [1704153600, 1704240000, 1704326400],
            "indicators": {
                "quote": [{"open": [80.0, None, 82.5], "high": [81.2, None, 83.9],
                           "low": [79.5, None, 82.0], "close": [81.0, None, 83.5],
                           "volume": [120000, None, 98000]}],
                "adjclose": [{"adjclose": [80.9, None, 83.4]}],
            }}]}}
        bars = provider._parse_chart("COMI", payload)
        # The null row is a market holiday or a gap: it is skipped, never
        # interpolated, because inventing a bar is fabricating a trade.
        assert len(bars) == 2
        assert all(b.close is not None for b in bars)
        assert bars == sorted(bars, key=lambda b: b.timestamp)

    def test_error_payload_raises(self):
        provider = YahooFinanceProvider()
        with pytest.raises(ProviderUnavailable):
            provider._parse_chart("X", {"chart": {"error": {"code": "Not Found"}}})

    def test_empty_result(self):
        assert YahooFinanceProvider()._parse_chart("X", {"chart": {"result": []}}) == []


class TestCsvProvider:
    @pytest.fixture
    def csv_dir(self, tmp_path: Path) -> Path:
        (tmp_path / "prices").mkdir()
        (tmp_path / "fundamentals").mkdir()
        (tmp_path / "prices" / "TEST.csv").write_text(
            "Date,Open,High,Low,Close,Volume\n"
            "2024-01-02,80.0,81.2,79.5,81.0,120000\n"
            "2024-01-03,81.0,,,,\n"                      # unusable row
            '2024-01-04,82.5,83.9,82.0,83.5,"98,000"\n'  # thousands separator
            "2024-01-07,83.5,84.0,83.0,84.2,n/a\n"       # junk volume
        )
        (tmp_path / "fundamentals" / "TEST.csv").write_text(
            "period,period_type,period_end,available_from,revenue,net_income,"
            "operating_cash_flow,capex,total_equity,eps\n"
            '2024-FY,FY,2024-12-31,2025-03-15,"1,200,000",150000,220000,(70000),900000,3.5\n'
            "2023-FY,FY,2023-12-31,2024-03-20,1050000,120000,190000,-60000,820000,2.8\n"
        )
        return tmp_path

    def test_skips_unusable_rows(self, csv_dir):
        bars = CsvFileProvider(csv_dir).get_price_history(
            "TEST", date(2020, 1, 1), date(2030, 1, 1)
        )
        assert len(bars) == 3   # the row with no close is dropped

    def test_handles_number_formats(self, csv_dir):
        bars = CsvFileProvider(csv_dir).get_price_history(
            "TEST", date(2020, 1, 1), date(2030, 1, 1)
        )
        by_date = {b.timestamp: b for b in bars}
        assert by_date[date(2024, 1, 4)].volume == 98_000.0
        assert by_date[date(2024, 1, 7)].volume is None  # junk -> None, never 0

    def test_source_records_the_file(self, csv_dir):
        bars = CsvFileProvider(csv_dir).get_price_history(
            "TEST", date(2020, 1, 1), date(2030, 1, 1)
        )
        assert "TEST.csv" in bars[0].quality.source

    def test_reads_publication_dates(self, csv_dir):
        statements = CsvFileProvider(csv_dir).get_financial_statements("TEST")
        assert statements[0].available_from == date(2025, 3, 15)

    def test_computes_fcf_when_absent(self, csv_dir):
        statements = CsvFileProvider(csv_dir).get_financial_statements("TEST")
        assert statements[0].free_cash_flow == pytest.approx(150_000.0)

    def test_quote_derived_from_bars(self, csv_dir):
        quote = CsvFileProvider(csv_dir).get_quote("TEST")
        assert quote.price == 84.2
        assert quote.week52_high is not None and quote.week52_low is not None

    def test_missing_files_yield_nothing(self, tmp_path):
        provider = CsvFileProvider(tmp_path)
        assert provider.get_price_history("NOPE", date(2020, 1, 1), date(2030, 1, 1)) == []

    def test_unavailable_when_directory_missing(self, tmp_path):
        provider = CsvFileProvider(tmp_path / "nonexistent")
        assert not provider.is_available()
        assert "not found" in provider.unavailable_reason()


class TestParsers:
    @pytest.mark.parametrize("raw,expected", [
        ("1,234.5", 1234.5), ("(70000)", -70000.0), ("1.5M", 1_500_000.0),
        ("2K", 2000.0), ("15%", 15.0), ("", None), ("n/a", None),
        ("#N/A", None), (None, None), ("abc", None),
    ])
    def test_parse_float(self, raw, expected):
        assert parse_float(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("2024-01-15", date(2024, 1, 15)), ("15/01/2024", date(2024, 1, 15)),
        ("20240115", date(2024, 1, 15)), ("", None), ("nonsense", None),
    ])
    def test_parse_date(self, raw, expected):
        assert parse_date(raw) == expected


class TestSentiment:
    def test_positive_and_negative(self):
        assert score_sentiment("record profit, revenue surged")[1] == "POSITIVE"
        assert score_sentiment("shares plunge after suspension")[1] == "NEGATIVE"

    def test_no_signal_returns_none_not_zero(self):
        # "no signal" and "neutral signal" are different claims.
        score, label = score_sentiment("Board meeting scheduled")
        assert score is None and label == "UNKNOWN"

    def test_bounded(self):
        score, _ = score_sentiment("profit " * 50)
        assert -1.0 <= score <= 1.0


class TestDisclosureClassification:
    @pytest.mark.parametrize("title,kind", [
        ("Board approves cash dividend", "DIVIDEND"),
        ("Acquisition of subsidiary completed", "M&A"),
        ("FY2024 financial statements", "EARNINGS"),
        ("Resignation of Chief Executive Officer", "MANAGEMENT_CHANGE"),
        ("Share buyback programme approved", "BUYBACK"),
        ("Weather update", "OTHER"),
    ])
    def test_classification(self, title, kind):
        assert classify_disclosure(title)[0] == kind

    def test_url_hash_is_stable_and_distinct(self):
        assert url_hash("a", "b") == url_hash("a", "b")
        assert url_hash("a", "b") != url_hash("a", "c")


class TestSyntheticGuards:
    def test_disabled_without_explicit_opt_in(self, monkeypatch):
        from backend.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "allow_synthetic_data", False)
        with pytest.raises(SyntheticProviderDisabled):
            SyntheticProvider()

    def test_every_record_is_stamped(self):
        provider = SyntheticProvider(force=True)
        bars = provider.get_price_history("DEMO", date(2024, 1, 1), date(2024, 3, 1))
        assert bars
        assert all(b.quality.source.startswith("SYNTHETIC_DEMO") for b in bars)
        assert all(b.quality.confidence.value == "UNVERIFIED" for b in bars)

    def test_respects_egx_trading_week(self):
        bars = SyntheticProvider(force=True).get_price_history(
            "DEMO", date(2024, 1, 1), date(2024, 3, 1)
        )
        assert all(b.timestamp.weekday() not in (4, 5) for b in bars)

    def test_reporting_lag_present(self):
        statements = SyntheticProvider(force=True).get_financial_statements("DEMO")
        for statement in statements:
            assert statement.available_from > statement.period_end

    def test_deterministic(self):
        a = SyntheticProvider(force=True).get_price_history("X", date(2024, 1, 1), date(2024, 2, 1))
        b = SyntheticProvider(force=True).get_price_history("X", date(2024, 1, 1), date(2024, 2, 1))
        assert [x.close for x in a] == [x.close for x in b]


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
class _FlakyProvider(MarketDataProvider):
    """Fails on demand, so failover and error reporting can be tested."""

    name = "flaky"

    def __init__(self, *, fail: bool = True, bars: list | None = None) -> None:
        self.fail = fail
        self.bars = bars or []

    def capabilities(self):
        return ProviderCapabilities(name=self.name, domains={"prices"})

    def get_price_history(self, ticker, start, end, *, symbol_hint=None):
        if self.fail:
            raise ProviderUnavailable("simulated outage")
        return self.bars


class TestIngestion:
    @pytest.fixture
    def universe(self, db):
        db.add(Company(ticker="TEST", name="Test", sector="Banks", exchange="EGX",
                       shares_outstanding=1_000_000_000, in_egx30=True))
        db.flush()

    def test_inserts_and_is_idempotent(self, db, universe):
        chain = ProviderChain([SyntheticProvider(force=True)], "prices")
        first = ingest_prices(db, tickers=["TEST"], chain=chain, lookback_days=200)
        assert first.inserted > 0
        count = db.scalar(select(func.count()).select_from(PriceBar))

        second = ingest_prices(db, tickers=["TEST"], chain=chain, lookback_days=200)
        assert second.inserted == 0
        assert second.skipped > 0
        assert db.scalar(select(func.count()).select_from(PriceBar)) == count

    def test_duplicate_bars_in_one_payload_are_dropped(self, db, universe):
        duplicate = PriceBarDTO(ticker="TEST", timestamp=date(2024, 1, 2), close=10.0)
        chain = ProviderChain(
            [_FlakyProvider(fail=False, bars=[duplicate, duplicate])], "prices"
        )
        result = ingest_prices(db, tickers=["TEST"], chain=chain)
        assert result.inserted == 1
        assert result.skipped == 1

    def test_bar_without_close_is_skipped(self, db, universe):
        chain = ProviderChain([_FlakyProvider(fail=False, bars=[
            PriceBarDTO(ticker="TEST", timestamp=date(2024, 1, 2), close=None),
            PriceBarDTO(ticker="TEST", timestamp=date(2024, 1, 3), close=11.0),
        ])], "prices")
        result = ingest_prices(db, tickers=["TEST"], chain=chain)
        assert result.inserted == 1 and result.skipped == 1

    def test_provider_failure_is_recorded_not_swallowed(self, db, universe):
        chain = ProviderChain([_FlakyProvider(fail=True)], "prices")
        result = ingest_prices(db, tickers=["TEST"], chain=chain)
        assert len(result.failures) == 1
        assert "simulated outage" in result.failures[0].message
        logged = db.execute(select(DataQualityLog)).scalars().all()
        assert any(row.status == "FAILED" for row in logged)

    def test_failover_to_second_provider(self, db, universe):
        good = _FlakyProvider(fail=False, bars=[
            PriceBarDTO(ticker="TEST", timestamp=date(2024, 1, 2), close=10.0)
        ])
        chain = ProviderChain([_FlakyProvider(fail=True), good], "prices")
        result = ingest_prices(db, tickers=["TEST"], chain=chain)
        assert result.inserted == 1

    def test_no_provider_reports_clearly(self, db, universe):
        result = ingest_prices(db, tickers=["TEST"], chain=ProviderChain([], "prices"))
        assert len(result.failures) == 1
        assert "No market data provider" in result.failures[0].message

    def test_fundamentals_default_publication_lag(self, db, universe):
        from backend.data.models import FinancialStatement
        from backend.data.providers.base import FinancialStatementDTO, FundamentalDataProvider

        class Provider(FundamentalDataProvider):
            name = "test"

            def capabilities(self):
                return ProviderCapabilities(name="test", domains={"fundamentals"})

            def get_financial_statements(self, ticker, *, limit=20, symbol_hint=None):
                return [FinancialStatementDTO(
                    ticker=ticker, period="2024-FY", period_type="FY",
                    period_end=date(2024, 12, 31), available_from=None, revenue=100.0,
                )]

        ingest_fundamentals(db, tickers=["TEST"], chain=ProviderChain([Provider()], "fundamentals"))
        statement = db.scalar(select(FinancialStatement))
        # Without an explicit publication date, a conservative 90-day statutory
        # lag is assumed so backtests never see it early.
        assert statement.available_from > statement.period_end

    def test_valuation_absent_without_share_count(self, db):
        db.add(Company(ticker="NOSHARES", name="X", exchange="EGX"))
        db.add(PriceBar(ticker="NOSHARES", timestamp=date.today(), close=10.0,
                        source="test", retrieved_at=datetime.now(timezone.utc)))
        db.flush()
        snapshot = compute_valuation_snapshot(db, "NOSHARES")
        assert snapshot.market_cap is None
        assert snapshot.pb is None   # never guessed

    def test_valuation_without_price_returns_nothing(self, db, universe):
        assert compute_valuation_snapshot(db, "TEST") is None
