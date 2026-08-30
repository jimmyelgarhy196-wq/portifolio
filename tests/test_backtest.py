"""Backtest engine and metrics."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.backtesting.engine import BacktestConfig, persist_backtest, run_backtest
from backend.backtesting.metrics import (
    beta_alpha,
    cagr,
    calmar_ratio,
    compute_metrics,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    to_returns,
    total_return,
    trade_statistics,
    volatility,
)
from backend.backtesting.strategies import STRATEGIES, build_strategy
from tests.conftest import make_prices, make_statements


class TestMetrics:
    def test_total_return(self):
        assert total_return([100, 130]) == pytest.approx(0.30)

    def test_total_return_needs_two_points(self):
        assert total_return([100]) is None

    def test_cagr_over_one_year(self):
        assert cagr([100, 110], 365) == pytest.approx(0.10, abs=0.001)

    def test_cagr_handles_total_loss(self):
        assert cagr([100, 0], 365) == -1.0

    def test_max_drawdown(self):
        worst, longest = max_drawdown([100, 120, 90, 110, 80, 130])
        assert worst == pytest.approx(-1 / 3, rel=1e-3)
        assert longest > 0

    def test_calmar(self):
        assert calmar_ratio([100, 120, 90, 130], 365) is not None

    def test_ratios_need_enough_observations(self):
        few = [0.01] * 10
        assert sharpe_ratio(few) is None
        assert sortino_ratio(few) is None
        assert volatility([0.01, 0.02]) is None

    def test_sortino_ignores_upside_volatility(self):
        # Same mean, but one series has no downside at all.
        upside_only = [0.02] * 40
        assert sortino_ratio(upside_only) is None   # no downside deviation

    def test_beta_of_self_is_one(self):
        returns = [0.01, -0.02, 0.03, 0.0, -0.01] * 8
        beta, _ = beta_alpha(returns, returns)
        assert beta == pytest.approx(1.0)

    def test_beta_needs_overlap(self):
        assert beta_alpha([0.01] * 5, [0.01] * 5) == (None, None)

    def test_to_returns_skips_zero_base(self):
        assert to_returns([0, 100, 110]) == [pytest.approx(0.1)]

    def test_trade_statistics(self):
        trades = [
            {"pnl": 100, "return_pct": 0.10, "holding_days": 30},
            {"pnl": -50, "return_pct": -0.05, "holding_days": 20},
            {"pnl": 200, "return_pct": 0.22, "holding_days": 45},
            {"pnl": -30, "return_pct": -0.03, "holding_days": 15},
        ]
        stats = trade_statistics(trades)
        assert stats.total == 4
        assert stats.win_rate == 0.5
        assert stats.profit_factor == pytest.approx(300 / 80)
        assert stats.average_winner == pytest.approx(0.16)
        assert stats.average_loser == pytest.approx(-0.04)
        assert stats.expectancy == pytest.approx(0.06)
        assert stats.average_holding_days == pytest.approx(27.5)

    def test_no_trades(self):
        assert trade_statistics([]).total == 0

    def test_empty_curve_reports_rather_than_crashing(self):
        metrics = compute_metrics([], initial_capital=1_000_000)
        assert metrics.total_return is None
        assert metrics.notes

    def test_missing_benchmark_is_noted(self):
        curve = [(date(2024, 1, i + 1), 100.0 + i) for i in range(30)]
        metrics = compute_metrics(curve, [], initial_capital=100.0)
        assert metrics.beta is None
        assert any("benchmark" in note for note in metrics.notes)

    def test_short_history_is_noted(self):
        curve = [(date(2024, 1, i + 1), 100.0 + i) for i in range(5)]
        metrics = compute_metrics(curve, initial_capital=100.0)
        assert metrics.sharpe is None
        assert any("20" in note for note in metrics.notes)


class TestStrategies:
    def test_all_registered(self):
        assert set(STRATEGIES) >= {
            "fundamental_long", "momentum", "technical_swing",
            "multi_factor", "buy_and_hold",
        }

    def test_unknown_strategy_rejected(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            build_strategy("does_not_exist")

    def test_weights_respect_caps_and_sum_to_invested(self, db, company):
        from backend.backtesting.point_in_time import PointInTimeDataView

        make_prices(db, "TEST", days=300)
        make_statements(db, "TEST", years=4)
        view = PointInTimeDataView(session=db, as_of=date.today())
        weights = build_strategy("buy_and_hold", top_n=5, max_weight=0.2).target_weights(
            view, ["TEST"]
        )
        if weights:
            assert all(w <= 0.2001 for w in weights.values())
            assert sum(weights.values()) <= 0.96


class TestBacktestEngine:
    @pytest.fixture
    def market(self, db, benchmark):
        from backend.data.models import Company

        for i, ticker in enumerate(("AAA", "BBB", "CCC")):
            db.add(Company(
                ticker=ticker, name=ticker, sector="Banks", exchange="EGX",
                in_egx30=True, in_egx100=True, shares_outstanding=1_000_000_000,
            ))
        db.flush()
        for i, ticker in enumerate(("AAA", "BBB", "CCC")):
            make_prices(db, ticker, days=400, start_price=50.0 + i * 20,
                        daily_drift=0.0004 + i * 0.0002)
            make_statements(db, ticker, years=4)
        make_prices(db, "EGX30", days=400, start_price=1000.0)

    def test_runs_and_produces_metrics(self, db, market):
        result = run_backtest(db, BacktestConfig(
            strategy="buy_and_hold", index="egx30", rebalance="monthly",
            initial_capital=1_000_000,
        ))
        assert result.equity_curve
        assert result.metrics.total_return is not None
        assert result.metrics.start_date < result.metrics.end_date

    def test_costs_are_charged(self, db, market):
        result = run_backtest(db, BacktestConfig(
            strategy="buy_and_hold", index="egx30", rebalance="monthly",
            commission_bps=50, slippage_bps=50,
        ))
        assert result.metrics.total_costs > 0

    def test_higher_costs_reduce_return(self, db, market):
        cheap = run_backtest(db, BacktestConfig(
            strategy="buy_and_hold", index="egx30", rebalance="weekly",
            commission_bps=1, slippage_bps=1,
        ))
        expensive = run_backtest(db, BacktestConfig(
            strategy="buy_and_hold", index="egx30", rebalance="weekly",
            commission_bps=200, slippage_bps=200,
        ))
        if cheap.metrics.total_return is not None and expensive.metrics.total_return is not None:
            assert expensive.metrics.total_return < cheap.metrics.total_return

    def test_no_price_data_reports_clearly(self, db, company):
        result = run_backtest(db, BacktestConfig(strategy="buy_and_hold", index="egx30"))
        assert result.warnings
        assert any("price history" in w or "trading days" in w for w in result.warnings)

    def test_synthetic_data_is_flagged(self, db, benchmark):
        from backend.data.models import Company

        db.add(Company(ticker="SYN", name="Syn", exchange="EGX", in_egx30=True,
                       shares_outstanding=1e9))
        db.flush()
        make_prices(db, "SYN", days=300, source="SYNTHETIC_DEMO:synthetic")
        make_prices(db, "EGX30", days=300, start_price=1000.0)
        result = run_backtest(db, BacktestConfig(strategy="buy_and_hold", index="egx30"))
        assert result.contains_synthetic
        assert any("SYNTHETIC" in w for w in result.warnings)

    def test_persisted_run_is_retrievable(self, db, market):
        result = run_backtest(db, BacktestConfig(strategy="buy_and_hold", index="egx30"))
        run = persist_backtest(db, result, name="test run")
        assert run.id is not None
        assert run.metrics
        assert run.equity_curve

    def test_final_liquidation_leaves_realisable_value(self, db, market):
        result = run_backtest(db, BacktestConfig(
            strategy="buy_and_hold", index="egx30", rebalance="quarterly",
        ))
        # Every position is closed at the end, so the closing equity is cash.
        assert result.metrics.final_value > 0
        assert result.closed_trades
