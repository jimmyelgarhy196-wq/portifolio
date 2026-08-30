"""Backtest engine.

Walks a trading calendar day by day, advancing a point-in-time cursor. At each
rebalance it asks the strategy for target weights *using only data available on
that date*, then trades toward them with realistic costs.

Execution convention: signals are computed on the close of day T and filled at
the open of day T+1. Filling at the same close that generated the signal would
be a subtle but decisive form of look-ahead, and it is the single most common
way a backtest flatters itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from sqlalchemy.orm import Session

from backend.backtesting.metrics import BacktestMetrics, compute_metrics
from backend.backtesting.point_in_time import (
    PointInTimeDataView,
    rebalance_dates,
    trading_calendar,
)
from backend.backtesting.strategies import Strategy, build_strategy
from backend.core.config import get_settings, load_yaml_config
from backend.core.logging_config import EVENT_BACKTEST, get_logger, log_event
from backend.data.models import BacktestRun, PriceBar
from backend.data.universe import get_universe

logger = get_logger(__name__)


@dataclass
class Holding:
    ticker: str
    quantity: float
    average_price: float
    opened_on: date


@dataclass
class ClosedTrade:
    ticker: str
    opened_on: date
    closed_on: date
    quantity: float
    entry_price: float
    exit_price: float
    pnl: float
    return_pct: float
    holding_days: int
    costs: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "opened_on": self.opened_on.isoformat(),
            "closed_on": self.closed_on.isoformat(),
            "quantity": round(self.quantity, 2),
            "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4),
            "pnl": round(self.pnl, 2),
            "return_pct": round(self.return_pct, 5),
            "holding_days": self.holding_days,
            "costs": round(self.costs, 2),
        }


@dataclass
class BacktestConfig:
    strategy: str = "fundamental_long"
    start: date | None = None
    end: date | None = None
    initial_capital: float = 1_000_000.0
    rebalance: str = "monthly"
    index: str = "egx30"
    benchmark: str | None = None
    commission_bps: float | None = None
    slippage_bps: float | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "initial_capital": self.initial_capital,
            "rebalance": self.rebalance,
            "index": self.index,
            "benchmark": self.benchmark,
            "commission_bps": self.commission_bps,
            "slippage_bps": self.slippage_bps,
            "params": self.params,
        }


@dataclass
class BacktestResult:
    config: BacktestConfig
    metrics: BacktestMetrics
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    benchmark_curve: list[tuple[date, float]] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    rebalance_log: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    contains_synthetic: bool = False

    def to_dict(self, *, include_curve: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "config": self.config.to_dict(),
            "metrics": self.metrics.to_dict(),
            "trades": [t.to_dict() for t in self.closed_trades],
            "rebalances": len(self.rebalance_log),
            "warnings": self.warnings,
            "contains_synthetic": self.contains_synthetic,
        }
        if include_curve:
            payload["equity_curve"] = [
                {"date": d.isoformat(), "value": round(v, 2)} for d, v in self.equity_curve
            ]
            payload["benchmark_curve"] = [
                {"date": d.isoformat(), "value": round(v, 4)}
                for d, v in self.benchmark_curve
            ]
        return payload


def run_backtest(
    session: Session, config: BacktestConfig, *, strategy: Strategy | None = None
) -> BacktestResult:
    """Execute a point-in-time backtest."""
    settings = get_settings()
    costs = load_yaml_config("risk").get("costs", {})
    commission_bps = (
        config.commission_bps if config.commission_bps is not None
        else float(costs.get("commission_bps", settings.commission_bps))
    )
    slippage_bps = (
        config.slippage_bps if config.slippage_bps is not None
        else float(costs.get("slippage_bps", settings.slippage_bps))
    )
    benchmark_ticker = config.benchmark or settings.benchmark_ticker

    strategy = strategy or build_strategy(config.strategy, **config.params)
    companies = get_universe(session, config.index)
    universe = [c.ticker for c in companies]

    result = BacktestResult(config=config, metrics=BacktestMetrics())
    if not universe:
        result.warnings.append("The requested universe is empty.")
        return result

    # --- Calendar ------------------------------------------------------------
    bounds = session.query(
        PriceBar.timestamp
    ).order_by(PriceBar.timestamp).limit(1).all()
    if not bounds:
        result.warnings.append(
            "No price history is stored. Ingest market data before backtesting."
        )
        return result

    start = config.start or bounds[0][0]
    end = config.end or date.today()
    calendar = trading_calendar(session, start, end)
    if len(calendar) < 30:
        result.warnings.append(
            f"Only {len(calendar)} trading days of price history in the requested "
            "range. A backtest needs materially more to mean anything."
        )
        if not calendar:
            return result

    rebalances = set(rebalance_dates(calendar, config.rebalance))

    # --- Simulation state ----------------------------------------------------
    cash = config.initial_capital
    holdings: dict[str, Holding] = {}
    pending_targets: dict[str, float] | None = None
    total_costs = 0.0
    traded_value = 0.0
    synthetic_seen = False

    for index, today in enumerate(calendar):
        view = PointInTimeDataView(session=session, as_of=today)

        # --- Execute yesterday's decision at today's open --------------------
        if pending_targets is not None:
            executed, cost, turnover_value = _rebalance(
                session, view, holdings, pending_targets, cash, today,
                commission_bps=commission_bps, slippage_bps=slippage_bps,
                closed_trades=result.closed_trades,
            )
            cash = executed
            total_costs += cost
            traded_value += turnover_value
            pending_targets = None

        # --- Mark to market ---------------------------------------------------
        market_value = 0.0
        for ticker, holding in list(holdings.items()):
            price = view.price(ticker, on=today)
            if price is None:
                continue
            market_value += price * holding.quantity
        equity = cash + market_value
        result.equity_curve.append((today, equity))

        benchmark_price = view.price(benchmark_ticker, on=today)
        if benchmark_price is not None:
            result.benchmark_curve.append((today, benchmark_price))

        # --- Decide on the close, for execution tomorrow ---------------------
        if today in rebalances and index < len(calendar) - 1:
            try:
                targets = strategy.target_weights(view, universe)
            except Exception as exc:  # noqa: BLE001 - a bad step must not kill the run
                result.warnings.append(f"{today.isoformat()}: strategy failed — {exc}")
                targets = {}
            pending_targets = targets
            result.rebalance_log.append({
                "date": today.isoformat(),
                "targets": {t: round(w, 4) for t, w in targets.items()},
                "equity": round(equity, 2),
            })
            bar = view.bar(next(iter(targets), universe[0]), on=today) if targets else None
            if bar is not None and "SYNTHETIC" in (bar.source or "").upper():
                synthetic_seen = True

    # --- Liquidate at the final close so the result is realisable ------------
    if holdings and calendar:
        final_view = PointInTimeDataView(session=session, as_of=calendar[-1])
        cash, cost, turnover_value = _rebalance(
            session, final_view, holdings, {}, cash, calendar[-1],
            commission_bps=commission_bps, slippage_bps=slippage_bps,
            closed_trades=result.closed_trades,
        )
        total_costs += cost
        traded_value += turnover_value
        if result.equity_curve:
            result.equity_curve[-1] = (calendar[-1], cash)

    average_equity = (
        sum(v for _, v in result.equity_curve) / len(result.equity_curve)
        if result.equity_curve else config.initial_capital
    )
    years = max((calendar[-1] - calendar[0]).days / 365.25, 1e-9) if calendar else 1.0
    turnover = (traded_value / average_equity / years) if average_equity > 0 else None

    result.metrics = compute_metrics(
        result.equity_curve, result.benchmark_curve,
        initial_capital=config.initial_capital,
        closed_trades=[t.to_dict() for t in result.closed_trades],
        total_costs=total_costs, turnover=turnover,
    )
    result.contains_synthetic = synthetic_seen or _uses_synthetic(session, universe[:3])
    if result.contains_synthetic:
        result.warnings.insert(
            0,
            "This backtest ran on SYNTHETIC DEMONSTRATION DATA. The results describe "
            "a fictional market and carry no information about any real strategy.",
        )

    log_event(
        logger, EVENT_BACKTEST,
        f"Backtest '{config.strategy}' complete: "
        f"{result.metrics.total_return:+.2%} total return"
        if result.metrics.total_return is not None else
        f"Backtest '{config.strategy}' complete (no return computed)",
        strategy=config.strategy, trades=len(result.closed_trades),
        rebalances=len(result.rebalance_log), synthetic=result.contains_synthetic,
    )
    return result


def _rebalance(
    session: Session,
    view: PointInTimeDataView,
    holdings: dict[str, Holding],
    targets: dict[str, float],
    cash: float,
    today: date,
    *,
    commission_bps: float,
    slippage_bps: float,
    closed_trades: list[ClosedTrade],
) -> tuple[float, float, float]:
    """Trade toward *targets*. Returns ``(cash, costs, traded_value)``."""
    commission_rate = commission_bps / 10_000.0
    slippage_rate = slippage_bps / 10_000.0

    prices: dict[str, float] = {}
    for ticker in set(list(holdings) + list(targets)):
        price = view.price(ticker, on=today)
        if price is not None and price > 0:
            prices[ticker] = price

    equity = cash + sum(
        prices[t] * h.quantity for t, h in holdings.items() if t in prices
    )
    total_costs = 0.0
    traded_value = 0.0

    # --- Sells first, so their proceeds fund the buys ------------------------
    for ticker, holding in list(holdings.items()):
        price = prices.get(ticker)
        if price is None:
            continue  # cannot trade what has no price; the position simply persists
        target_quantity = (targets.get(ticker, 0.0) * equity) / price
        if target_quantity >= holding.quantity - 1e-9:
            continue
        sell_quantity = holding.quantity - target_quantity
        fill = price * (1.0 - slippage_rate)
        gross = fill * sell_quantity
        commission = gross * commission_rate
        cash += gross - commission
        total_costs += commission + (price - fill) * sell_quantity
        traded_value += gross

        pnl = (fill - holding.average_price) * sell_quantity - commission
        closed_trades.append(ClosedTrade(
            ticker=ticker, opened_on=holding.opened_on, closed_on=today,
            quantity=sell_quantity, entry_price=holding.average_price, exit_price=fill,
            pnl=pnl,
            return_pct=(
                (fill - holding.average_price) / holding.average_price
                if holding.average_price else 0.0
            ),
            holding_days=(today - holding.opened_on).days,
            costs=commission,
        ))
        holding.quantity = target_quantity
        if holding.quantity <= 1e-9:
            del holdings[ticker]

    # --- Then buys ------------------------------------------------------------
    for ticker, weight in sorted(targets.items(), key=lambda kv: -kv[1]):
        price = prices.get(ticker)
        if price is None or weight <= 0:
            continue
        target_value = weight * equity
        current_quantity = holdings[ticker].quantity if ticker in holdings else 0.0
        current_value = current_quantity * price
        if target_value <= current_value + 1e-9:
            continue

        fill = price * (1.0 + slippage_rate)
        buy_value = min(target_value - current_value, cash / (1.0 + commission_rate))
        if buy_value <= 0:
            continue
        buy_quantity = buy_value / fill
        gross = fill * buy_quantity
        commission = gross * commission_rate
        if gross + commission > cash + 1e-9:
            continue
        cash -= gross + commission
        total_costs += commission + (fill - price) * buy_quantity
        traded_value += gross

        if ticker in holdings:
            holding = holdings[ticker]
            total_cost = holding.average_price * holding.quantity + gross
            holding.quantity += buy_quantity
            holding.average_price = total_cost / holding.quantity
        else:
            holdings[ticker] = Holding(
                ticker=ticker, quantity=buy_quantity, average_price=fill, opened_on=today
            )

    return cash, total_costs, traded_value


def _uses_synthetic(session: Session, tickers: Sequence[str]) -> bool:
    from sqlalchemy import select

    for ticker in tickers:
        source = session.scalar(
            select(PriceBar.source).where(PriceBar.ticker == ticker.upper()).limit(1)
        )
        if source and "SYNTHETIC" in source.upper():
            return True
    return False


def persist_backtest(
    session: Session, result: BacktestResult, *, name: str | None = None
) -> BacktestRun:
    """Store a backtest so it can be compared with others later."""
    config = result.config
    run = BacktestRun(
        name=name or f"{config.strategy} {config.index} {config.rebalance}",
        strategy=config.strategy,
        start_date=result.metrics.start_date or config.start or date.today(),
        end_date=result.metrics.end_date or config.end or date.today(),
        initial_capital=config.initial_capital,
        parameters=config.to_dict(),
        metrics=result.metrics.to_dict(),
        equity_curve=[
            {"date": d.isoformat(), "value": round(v, 2)} for d, v in result.equity_curve
        ],
        trades=[t.to_dict() for t in result.closed_trades],
        contains_synthetic_data=result.contains_synthetic,
    )
    session.add(run)
    session.flush()
    return run


def compare_strategies(
    session: Session,
    strategies: Sequence[str],
    *,
    base_config: BacktestConfig | None = None,
) -> dict[str, BacktestResult]:
    """Run several strategies over identical conditions for a fair comparison."""
    base = base_config or BacktestConfig()
    out: dict[str, BacktestResult] = {}
    for name in strategies:
        config = BacktestConfig(
            strategy=name, start=base.start, end=base.end,
            initial_capital=base.initial_capital, rebalance=base.rebalance,
            index=base.index, benchmark=base.benchmark,
            commission_bps=base.commission_bps, slippage_bps=base.slippage_bps,
            params=dict(base.params),
        )
        out[name] = run_backtest(session, config)
    return out
