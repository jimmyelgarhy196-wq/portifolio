"""Position sizing, paper trading, risk and attribution."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.portfolio.paper_trading import (
    FillModel,
    LiveTradingBlocked,
    TradeRejected,
    close_position,
    execute_trade,
    get_or_create_portfolio,
    mark_to_market,
    snapshot_portfolio,
)
from backend.portfolio.risk import (
    analyze_risk,
    annualized_volatility,
    compute_beta,
    correlation,
    historical_var,
    max_drawdown,
)
from backend.portfolio.sizing import SizingInputs, compute_risk_reward, size_position
from tests.conftest import make_prices


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------
class TestRiskReward:
    def test_long(self):
        assert compute_risk_reward(100, 130, 90, "LONG") == pytest.approx(3.0)

    def test_short(self):
        assert compute_risk_reward(100, 80, 110, "SHORT") == pytest.approx(2.0)

    def test_incoherent_levels_rejected(self):
        assert compute_risk_reward(100, 90, 95, "LONG") is None    # target below entry
        assert compute_risk_reward(100, 130, 110, "LONG") is None  # stop above entry

    def test_missing_inputs(self):
        assert compute_risk_reward(None, 130, 90) is None


class TestSizing:
    BASE = dict(
        ticker="TEST", conviction=8.0, entry_price=100.0, target_price=130.0,
        invalidation_price=90.0, annual_volatility=0.30, portfolio_value=1_000_000.0,
        current_cash_weight=1.0,
    )

    def test_produces_a_size_with_reasoning(self):
        result = size_position(SizingInputs(**self.BASE))
        assert not result.rejected
        assert 0 < result.recommended_weight <= 0.20
        assert result.recommended_value == pytest.approx(
            result.recommended_weight * 1_000_000
        )
        assert result.recommended_quantity > 0
        assert len(result.steps) >= 4
        assert "Recommended allocation" in result.explain()

    def test_conviction_scales_size(self):
        low = size_position(SizingInputs(**{**self.BASE, "conviction": 5.0}))
        high = size_position(SizingInputs(**{**self.BASE, "conviction": 9.5}))
        assert high.recommended_weight > low.recommended_weight

    def test_below_conviction_floor_rejected(self):
        result = size_position(SizingInputs(**{**self.BASE, "conviction": 2.0}))
        assert result.rejected and "conviction" in result.rejection_reason.lower()

    def test_missing_conviction_rejected(self):
        result = size_position(SizingInputs(**{**self.BASE, "conviction": None}))
        assert result.rejected

    def test_poor_risk_reward_rejected(self):
        result = size_position(SizingInputs(
            **{**self.BASE, "target_price": 105.0, "invalidation_price": 90.0}
        ))
        assert result.rejected and "risk/reward" in result.rejection_reason.lower()

    def test_high_volatility_reduces_size(self):
        calm = size_position(SizingInputs(**{**self.BASE, "annual_volatility": 0.15}))
        wild = size_position(SizingInputs(**{**self.BASE, "annual_volatility": 0.85}))
        assert wild.recommended_weight < calm.recommended_weight
        assert wild.binding_constraint == "volatility"

    def test_risk_per_position_capped(self):
        # Invalidation 30% away: a 2% risk budget caps the position near 6.7%.
        result = size_position(SizingInputs(
            **{**self.BASE, "conviction": 10.0, "invalidation_price": 70.0,
               "target_price": 200.0}
        ))
        assert result.risk_per_position <= 0.0201

    def test_sector_limit_binds(self):
        result = size_position(SizingInputs(
            **{**self.BASE, "current_sector_weight": 0.28}
        ))
        assert result.recommended_weight <= 0.0201
        assert result.binding_constraint == "sector_limit"

    def test_correlation_reduces_size(self):
        alone = size_position(SizingInputs(**self.BASE))
        crowded = size_position(SizingInputs(**{**self.BASE, "correlated_holdings": 2}))
        assert crowded.recommended_weight < alone.recommended_weight

    def test_cash_floor_binds(self):
        result = size_position(SizingInputs(**{**self.BASE, "current_cash_weight": 0.07}))
        assert result.recommended_weight <= 0.0201
        assert result.binding_constraint == "cash_floor"

    def test_illiquidity_binds(self):
        result = size_position(SizingInputs(
            **{**self.BASE, "average_turnover": 100_000.0}
        ))
        assert result.binding_constraint == "liquidity"

    def test_speculative_bucket_binds(self):
        result = size_position(SizingInputs(
            **{**self.BASE, "strategy": "technical_swing",
               "current_speculative_weight": 0.14}
        ))
        assert result.binding_constraint == "speculative_limit"

    def test_tiny_position_rejected(self):
        result = size_position(SizingInputs(
            **{**self.BASE, "current_cash_weight": 0.055}
        ))
        assert result.rejected and "minimum" in result.rejection_reason.lower()

    def test_never_exceeds_max_position(self):
        result = size_position(SizingInputs(
            **{**self.BASE, "conviction": 10.0, "annual_volatility": 0.05,
               "target_price": 400.0, "invalidation_price": 99.0}
        ))
        assert result.recommended_weight <= 0.20


# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------
class TestFillModel:
    def test_slippage_always_hurts(self):
        model = FillModel(commission_bps=20, slippage_bps=15)
        assert model.fill_price(100.0, "BUY") > 100.0
        assert model.fill_price(100.0, "SELL") < 100.0
        assert model.fill_price(100.0, "SHORT") < 100.0
        assert model.fill_price(100.0, "COVER") > 100.0

    def test_commission_scales(self):
        model = FillModel(commission_bps=20, slippage_bps=0)
        assert model.commission(100_000) == pytest.approx(200.0)


class TestPaperTrading:
    def test_portfolio_starts_in_paper_mode(self, db):
        portfolio = get_or_create_portfolio(db)
        assert portfolio.mode == "PAPER"
        assert portfolio.cash == portfolio.initial_capital

    def test_buy_reduces_cash_and_creates_position(self, db, company):
        make_prices(db, "TEST", days=60, start_price=100.0)
        portfolio = get_or_create_portfolio(db)
        before = portfolio.cash
        trade = execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=100)
        assert portfolio.cash < before
        assert trade.commission > 0
        assert trade.mode == "PAPER"
        state = mark_to_market(db, portfolio)
        assert len(state["positions"]) == 1

    def test_sell_realises_pnl(self, db, company):
        make_prices(db, "TEST", days=60, start_price=100.0)
        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=100)
        trade = execute_trade(db, portfolio, ticker="TEST", side="SELL", quantity=100)
        assert trade.realized_pnl is not None
        assert not mark_to_market(db, portfolio)["positions"]

    def test_average_price_updates_on_add(self, db, company):
        make_prices(db, "TEST", days=60, start_price=100.0)
        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=100, price=50.0)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=100, price=100.0)
        position = mark_to_market(db, portfolio)["positions"][0]
        assert 70 < position.average_price < 80

    def test_paper_short_and_cover(self, db, company):
        make_prices(db, "TEST", days=60, start_price=100.0)
        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="TEST", side="SHORT", quantity=100)
        position = mark_to_market(db, portfolio)["positions"][0]
        assert position.direction == "SHORT"
        assert position.market_value < 0    # a liability, not an asset
        trade = execute_trade(db, portfolio, ticker="TEST", side="COVER", quantity=100)
        assert trade.realized_pnl is not None

    def test_cannot_sell_more_than_held(self, db, company):
        make_prices(db, "TEST", days=60)
        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=10)
        with pytest.raises(TradeRejected, match="only"):
            execute_trade(db, portfolio, ticker="TEST", side="SELL", quantity=1000)

    def test_cannot_exceed_cash(self, db, company):
        make_prices(db, "TEST", days=60, start_price=100.0)
        portfolio = get_or_create_portfolio(db)
        with pytest.raises(TradeRejected, match="Insufficient cash"):
            execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=10_000_000)

    def test_cannot_trade_without_a_price(self, db):
        portfolio = get_or_create_portfolio(db)
        with pytest.raises(TradeRejected, match="No price available"):
            execute_trade(db, portfolio, ticker="GHOST", side="BUY", quantity=10)

    def test_rejects_bad_input(self, db, company):
        make_prices(db, "TEST", days=60)
        portfolio = get_or_create_portfolio(db)
        with pytest.raises(TradeRejected):
            execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=-5)
        with pytest.raises(TradeRejected):
            execute_trade(db, portfolio, ticker="TEST", side="TELEPORT", quantity=5)

    def test_live_mode_is_blocked(self, db, company):
        make_prices(db, "TEST", days=60)
        portfolio = get_or_create_portfolio(db)
        portfolio.mode = "LIVE"
        with pytest.raises(LiveTradingBlocked):
            execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=1)

    def test_close_position(self, db, company):
        make_prices(db, "TEST", days=60)
        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=100)
        assert close_position(db, portfolio, "TEST") is not None
        assert not mark_to_market(db, portfolio)["positions"]

    def test_unpriced_positions_are_reported_not_ignored(self, db, company):
        from backend.data.models import Position

        portfolio = get_or_create_portfolio(db)
        db.add(Position(
            portfolio_id=portfolio.portfolio_id, ticker="GHOST", direction="LONG",
            quantity=100, average_price=10.0,
        ))
        db.flush()
        assert "GHOST" in mark_to_market(db, portfolio)["unpriced_tickers"]


# ---------------------------------------------------------------------------
# Risk statistics
# ---------------------------------------------------------------------------
class TestRiskStatistics:
    def test_volatility_needs_enough_points(self):
        assert annualized_volatility([0.01, 0.02]) is None
        assert annualized_volatility([0.01, -0.01] * 20) is not None

    def test_beta_of_identical_series_is_one(self):
        returns = [0.01, -0.02, 0.015, 0.005, -0.01] * 8
        assert compute_beta(returns, returns) == pytest.approx(1.0)

    def test_beta_needs_overlap(self):
        assert compute_beta([0.01] * 5, [0.01] * 5) is None

    def test_max_drawdown(self):
        worst, current = max_drawdown([100, 120, 90, 110, 80, 130])
        assert worst == pytest.approx(-1 / 3, rel=1e-3)
        assert current == pytest.approx(0.0)

    def test_var_needs_history(self):
        assert historical_var([0.01] * 10) is None
        assert historical_var([0.01, -0.03] * 15) is not None

    def test_correlation_bounds(self):
        a = [0.01, -0.02, 0.03, -0.01, 0.02] * 6
        assert correlation(a, a) == pytest.approx(1.0)
        assert correlation(a, [-x for x in a]) == pytest.approx(-1.0)


class TestRiskReport:
    def test_flags_sector_concentration(self, db, benchmark):
        from backend.data.models import Company

        for ticker in ("BANKA", "BANKB"):
            db.add(Company(ticker=ticker, name=ticker, sector="Banks", exchange="EGX"))
        db.flush()
        for ticker in ("BANKA", "BANKB"):
            make_prices(db, ticker, days=60, start_price=100.0)
        make_prices(db, "EGX30", days=60, start_price=1000.0)

        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="BANKA", side="BUY", quantity=2500,
                      strategy="fundamental_long")
        execute_trade(db, portfolio, ticker="BANKB", side="BUY", quantity=2500,
                      strategy="fundamental_long")

        report = analyze_risk(db, portfolio)
        codes = {w.code for w in report.warnings}
        assert "SECTOR_CONCENTRATION" in codes
        assert report.worst_severity in ("warning", "critical")

    def test_reports_unavailable_measures_rather_than_assuming_benign(self, db, company):
        make_prices(db, "TEST", days=60)
        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=100)
        report = analyze_risk(db, portfolio)
        # Without snapshot history, these cannot be computed and must say so.
        assert report.portfolio_volatility is None
        assert any("snapshot" in note for note in report.unavailable)

    def test_empty_portfolio_has_no_warnings(self, db):
        portfolio = get_or_create_portfolio(db)
        report = analyze_risk(db, portfolio)
        assert report.position_count == 0
        assert not [w for w in report.warnings if w.severity == "critical"]


class TestAttribution:
    def test_attributes_by_strategy(self, db, company, benchmark):
        from backend.portfolio.attribution import analyze_attribution

        make_prices(db, "TEST", days=90, start_price=100.0)
        make_prices(db, "EGX30", days=90, start_price=1000.0)
        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=100,
                      strategy="fundamental_long")

        report = analyze_attribution(db, portfolio)
        assert report.by_strategy
        assert report.by_strategy[0].name == "fundamental_long"
        assert report.best_position.ticker == "TEST"

    def test_snapshot_enables_period_returns(self, db, company, benchmark):
        from backend.portfolio.attribution import analyze_attribution

        make_prices(db, "TEST", days=90, start_price=100.0)
        make_prices(db, "EGX30", days=90, start_price=1000.0)
        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=100)
        for offset in (30, 20, 10, 0):
            snapshot_portfolio(db, portfolio, as_of=date.today() - timedelta(days=offset))
        report = analyze_attribution(db, portfolio)
        assert report.period_returns["monthly"] is not None
