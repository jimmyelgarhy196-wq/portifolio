"""Backtest performance metrics.

Pure functions over an equity curve and a benchmark series. Every metric that
needs more data than it has returns ``None`` rather than a number computed from
too few observations — a Sharpe ratio from six days is not a Sharpe ratio.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

TRADING_DAYS = 252

#: Egyptian risk-free proxy. EGX backtests measured against a 0% risk-free rate
#: would flatter Sharpe badly given Egyptian policy rates, so this is explicit
#: and configurable rather than assumed away.
DEFAULT_RISK_FREE_ANNUAL = 0.18


def to_returns(values: Sequence[float]) -> list[float]:
    out: list[float] = []
    for prev, curr in zip(values, values[1:]):
        if prev and prev > 0:
            out.append((curr - prev) / prev)
    return out


def total_return(values: Sequence[float]) -> float | None:
    if len(values) < 2 or not values[0]:
        return None
    return (values[-1] - values[0]) / values[0]


def cagr(values: Sequence[float], days: int) -> float | None:
    total = total_return(values)
    if total is None or days <= 0:
        return None
    years = days / 365.25
    if years <= 0:
        return None
    growth = 1.0 + total
    if growth <= 0:
        return -1.0
    return growth ** (1.0 / years) - 1.0


def volatility(returns: Sequence[float], *, annualize: bool = True) -> float | None:
    if len(returns) < 5:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    return std * math.sqrt(TRADING_DAYS) if annualize else std


def sharpe_ratio(
    returns: Sequence[float], risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL
) -> float | None:
    if len(returns) < 20:
        return None
    daily_rf = risk_free_annual / TRADING_DAYS
    excess = [r - daily_rf for r in returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    std = math.sqrt(variance)
    if std <= 0:
        return None
    return (mean / std) * math.sqrt(TRADING_DAYS)


def sortino_ratio(
    returns: Sequence[float], risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL
) -> float | None:
    """Like Sharpe, but penalising only downside deviation."""
    if len(returns) < 20:
        return None
    daily_rf = risk_free_annual / TRADING_DAYS
    excess = [r - daily_rf for r in returns]
    mean = sum(excess) / len(excess)
    downside = [r for r in excess if r < 0]
    if not downside:
        return None
    downside_deviation = math.sqrt(sum(r**2 for r in downside) / len(excess))
    if downside_deviation <= 0:
        return None
    return (mean / downside_deviation) * math.sqrt(TRADING_DAYS)


def max_drawdown(values: Sequence[float]) -> tuple[float | None, int | None]:
    """Returns ``(max_drawdown, longest_underwater_days)``."""
    if len(values) < 2:
        return None, None
    peak = values[0]
    worst = 0.0
    underwater = 0
    longest = 0
    for value in values:
        if value > peak:
            peak = value
            underwater = 0
        else:
            underwater += 1
            longest = max(longest, underwater)
        if peak > 0:
            worst = min(worst, (value - peak) / peak)
    return worst, longest


def calmar_ratio(values: Sequence[float], days: int) -> float | None:
    growth = cagr(values, days)
    drawdown, _ = max_drawdown(values)
    if growth is None or not drawdown:
        return None
    return growth / abs(drawdown)


def beta_alpha(
    returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL,
) -> tuple[float | None, float | None]:
    """CAPM beta and annualised Jensen's alpha."""
    n = min(len(returns), len(benchmark_returns))
    if n < 20:
        return None, None
    p, b = list(returns[-n:]), list(benchmark_returns[-n:])
    p_mean, b_mean = sum(p) / n, sum(b) / n
    covariance = sum((pi - p_mean) * (bi - b_mean) for pi, bi in zip(p, b)) / (n - 1)
    variance = sum((bi - b_mean) ** 2 for bi in b) / (n - 1)
    if variance <= 0:
        return None, None
    beta = covariance / variance
    daily_rf = risk_free_annual / TRADING_DAYS
    alpha_daily = (p_mean - daily_rf) - beta * (b_mean - daily_rf)
    return beta, alpha_daily * TRADING_DAYS


def information_ratio(
    returns: Sequence[float], benchmark_returns: Sequence[float]
) -> float | None:
    n = min(len(returns), len(benchmark_returns))
    if n < 20:
        return None
    active = [p - b for p, b in zip(returns[-n:], benchmark_returns[-n:])]
    mean = sum(active) / n
    variance = sum((a - mean) ** 2 for a in active) / (n - 1)
    tracking_error = math.sqrt(variance)
    if tracking_error <= 0:
        return None
    return (mean / tracking_error) * math.sqrt(TRADING_DAYS)


@dataclass
class TradeStats:
    total: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    average_winner: float | None = None
    average_loser: float | None = None
    largest_winner: float | None = None
    largest_loser: float | None = None
    profit_factor: float | None = None
    average_holding_days: float | None = None
    expectancy: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            k: (round(v, 4) if isinstance(v, float) else v)
            for k, v in self.__dict__.items()
        }


def trade_statistics(closed_trades: Sequence[dict[str, Any]]) -> TradeStats:
    """Win rate, average winner/loser, profit factor and expectancy."""
    stats = TradeStats(total=len(closed_trades))
    if not closed_trades:
        return stats

    pnls = [t.get("pnl", 0.0) for t in closed_trades]
    returns = [t.get("return_pct") for t in closed_trades if t.get("return_pct") is not None]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]

    stats.wins = len(winners)
    stats.losses = len(losers)
    decided = stats.wins + stats.losses
    stats.win_rate = stats.wins / decided if decided else None

    winning_returns = [r for r in returns if r > 0]
    losing_returns = [r for r in returns if r < 0]
    stats.average_winner = sum(winning_returns) / len(winning_returns) if winning_returns else None
    stats.average_loser = sum(losing_returns) / len(losing_returns) if losing_returns else None
    stats.largest_winner = max(returns) if returns else None
    stats.largest_loser = min(returns) if returns else None

    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    stats.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    holds = [t.get("holding_days") for t in closed_trades if t.get("holding_days") is not None]
    stats.average_holding_days = sum(holds) / len(holds) if holds else None

    if stats.win_rate is not None and stats.average_winner is not None and stats.average_loser is not None:
        stats.expectancy = (
            stats.win_rate * stats.average_winner
            + (1 - stats.win_rate) * stats.average_loser
        )
    return stats


@dataclass
class BacktestMetrics:
    start_date: date | None = None
    end_date: date | None = None
    days: int = 0
    initial_capital: float = 0.0
    final_value: float = 0.0
    total_return: float | None = None
    cagr: float | None = None
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    longest_drawdown_days: int | None = None
    calmar: float | None = None
    beta: float | None = None
    alpha: float | None = None
    information_ratio: float | None = None
    benchmark_return: float | None = None
    benchmark_cagr: float | None = None
    excess_return: float | None = None
    turnover: float | None = None
    total_costs: float = 0.0
    trades: TradeStats = field(default_factory=TradeStats)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "days": self.days,
            "initial_capital": round(self.initial_capital, 2),
            "final_value": round(self.final_value, 2),
            "total_costs": round(self.total_costs, 2),
            "trades": self.trades.to_dict(),
            "notes": self.notes,
        }
        for key in (
            "total_return", "cagr", "volatility", "sharpe", "sortino", "max_drawdown",
            "calmar", "beta", "alpha", "information_ratio", "benchmark_return",
            "benchmark_cagr", "excess_return", "turnover",
        ):
            value = getattr(self, key)
            payload[key] = None if value is None else round(value, 5)
        payload["longest_drawdown_days"] = self.longest_drawdown_days
        return payload


def compute_metrics(
    equity_curve: Sequence[tuple[date, float]],
    benchmark_curve: Sequence[tuple[date, float]] = (),
    *,
    initial_capital: float,
    closed_trades: Sequence[dict[str, Any]] = (),
    total_costs: float = 0.0,
    turnover: float | None = None,
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL,
) -> BacktestMetrics:
    """Compute the full metric suite from an equity curve."""
    metrics = BacktestMetrics(
        initial_capital=initial_capital,
        total_costs=total_costs,
        turnover=turnover,
        trades=trade_statistics(closed_trades),
    )
    if len(equity_curve) < 2:
        metrics.notes.append(
            "The equity curve has fewer than two points; no performance metrics "
            "could be computed."
        )
        return metrics

    dates = [d for d, _ in equity_curve]
    values = [v for _, v in equity_curve]
    metrics.start_date, metrics.end_date = dates[0], dates[-1]
    metrics.days = (dates[-1] - dates[0]).days
    metrics.final_value = values[-1]

    returns = to_returns(values)
    metrics.total_return = total_return(values)
    metrics.cagr = cagr(values, metrics.days)
    metrics.volatility = volatility(returns)
    metrics.sharpe = sharpe_ratio(returns, risk_free_annual)
    metrics.sortino = sortino_ratio(returns, risk_free_annual)
    metrics.max_drawdown, metrics.longest_drawdown_days = max_drawdown(values)
    metrics.calmar = calmar_ratio(values, metrics.days)

    if len(returns) < 20:
        metrics.notes.append(
            f"Only {len(returns)} return observations: Sharpe, Sortino, beta and alpha "
            "require at least 20 and were not computed."
        )

    # --- Benchmark comparison ------------------------------------------------
    if len(benchmark_curve) >= 2:
        aligned = _align(equity_curve, benchmark_curve)
        if len(aligned) >= 2:
            benchmark_values = [b for _, _, b in aligned]
            metrics.benchmark_return = total_return(benchmark_values)
            metrics.benchmark_cagr = cagr(benchmark_values, metrics.days)
            if metrics.total_return is not None and metrics.benchmark_return is not None:
                metrics.excess_return = metrics.total_return - metrics.benchmark_return
            portfolio_returns = to_returns([p for _, p, _ in aligned])
            benchmark_returns = to_returns(benchmark_values)
            metrics.beta, metrics.alpha = beta_alpha(
                portfolio_returns, benchmark_returns, risk_free_annual=risk_free_annual
            )
            metrics.information_ratio = information_ratio(
                portfolio_returns, benchmark_returns
            )
    else:
        metrics.notes.append(
            "No benchmark series was available, so beta, alpha, information ratio and "
            "excess return could not be computed."
        )
    return metrics


def _align(
    equity: Sequence[tuple[date, float]], benchmark: Sequence[tuple[date, float]]
) -> list[tuple[date, float, float]]:
    """Align two series on common dates, carrying the last known benchmark value."""
    lookup = dict(benchmark)
    out: list[tuple[date, float, float]] = []
    last: float | None = None
    for day, value in equity:
        if day in lookup:
            last = lookup[day]
        if last is not None:
            out.append((day, value, last))
    return out
