#!/usr/bin/env python3
"""Run a backtest and print the metrics."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.backtesting.engine import (  # noqa: E402
    BacktestConfig,
    compare_strategies,
    persist_backtest,
    run_backtest,
)
from backend.backtesting.strategies import STRATEGIES  # noqa: E402
from backend.core.database import init_database, session_scope  # noqa: E402
from backend.core.logging_config import configure_logging  # noqa: E402


def _fmt(value, pct: bool = False, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}%}" if pct else f"{value:.{digits}f}"


def print_metrics(name: str, metrics) -> None:
    m = metrics
    print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
    print(f"  Period            {m.start_date} → {m.end_date}  ({m.days} days)")
    print(f"  Total return      {_fmt(m.total_return, True)}    benchmark {_fmt(m.benchmark_return, True)}")
    print(f"  CAGR              {_fmt(m.cagr, True)}    excess    {_fmt(m.excess_return, True)}")
    print(f"  Volatility        {_fmt(m.volatility, True)}")
    print(f"  Sharpe / Sortino  {_fmt(m.sharpe)} / {_fmt(m.sortino)}")
    print(f"  Max drawdown      {_fmt(m.max_drawdown, True)}    Calmar {_fmt(m.calmar)}")
    print(f"  Beta / Alpha      {_fmt(m.beta)} / {_fmt(m.alpha, True)}")
    print(f"  Information ratio {_fmt(m.information_ratio)}")
    t = m.trades
    print(f"  Trades            {t.total}  win rate {_fmt(t.win_rate, True, 1)}")
    print(f"  Avg win / loss    {_fmt(t.average_winner, True)} / {_fmt(t.average_loser, True)}")
    print(f"  Profit factor     {_fmt(t.profit_factor)}    expectancy {_fmt(t.expectancy, True)}")
    print(f"  Turnover / costs  {_fmt(m.turnover)}x / {m.total_costs:,.0f}")
    for note in m.notes:
        print(f"  ⚠ {note}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default="fundamental_long", choices=sorted(STRATEGIES))
    parser.add_argument("--compare", action="store_true", help="run every strategy and compare")
    parser.add_argument("--index", default="egx30")
    parser.add_argument("--rebalance", default="monthly",
                        choices=["daily", "weekly", "monthly", "quarterly", "yearly"])
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    configure_logging()
    init_database()

    config = BacktestConfig(
        strategy=args.strategy, index=args.index, rebalance=args.rebalance,
        initial_capital=args.capital,
        start=date.fromisoformat(args.start) if args.start else None,
        end=date.fromisoformat(args.end) if args.end else None,
        params={"top_n": args.top_n},
    )

    with session_scope() as session:
        if args.compare:
            results = compare_strategies(session, sorted(STRATEGIES), base_config=config)
            for name, result in results.items():
                print_metrics(f"{name}  ({args.index}, {args.rebalance})", result.metrics)
                if not args.no_save:
                    persist_backtest(session, result)

            print(f"\n{'=' * 70}\nCOMPARISON\n{'=' * 70}")
            print(f"  {'STRATEGY':<20}{'RETURN':>10}{'CAGR':>10}{'SHARPE':>9}{'MAXDD':>9}{'TRADES':>8}")
            ranked = sorted(
                results.items(),
                key=lambda kv: kv[1].metrics.total_return
                if kv[1].metrics.total_return is not None else -99,
                reverse=True,
            )
            for name, result in ranked:
                m = result.metrics
                print(
                    f"  {name:<20}{_fmt(m.total_return, True):>10}{_fmt(m.cagr, True):>10}"
                    f"{_fmt(m.sharpe):>9}{_fmt(m.max_drawdown, True, 1):>9}{m.trades.total:>8}"
                )
            if any(r.contains_synthetic for r in results.values()):
                print("\n  ⚠ These results were produced on SYNTHETIC data and are meaningless.")
        else:
            result = run_backtest(session, config)
            print_metrics(f"{args.strategy}  ({args.index}, {args.rebalance})", result.metrics)
            for warning in result.warnings:
                print(f"\n  ⚠ {warning}")
            if not args.no_save:
                run = persist_backtest(session, result)
                print(f"\n  Saved as backtest run #{run.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
