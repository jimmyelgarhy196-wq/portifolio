"""Performance attribution.

Answers the question the brief cares most about: *which strategy actually
works?* Attribution is computed from the trade ledger and current positions, so
contribution by strategy, sector and individual name is always reconcilable back
to real recorded fills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.data_quality import safe_div
from backend.data.models import Portfolio, PortfolioSnapshot, Position, PriceBar, Trade


@dataclass
class PositionContribution:
    ticker: str
    strategy: str | None
    sector: str | None
    direction: str
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    cost_basis: float
    return_on_cost: float | None
    contribution_to_return: float | None
    is_open: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "strategy": self.strategy,
            "sector": self.sector,
            "direction": self.direction,
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "total_pnl": round(self.total_pnl, 2),
            "cost_basis": round(self.cost_basis, 2),
            "return_on_cost": (
                None if self.return_on_cost is None else round(self.return_on_cost, 4)
            ),
            "contribution_to_return": (
                None if self.contribution_to_return is None
                else round(self.contribution_to_return, 5)
            ),
            "is_open": self.is_open,
        }


@dataclass
class GroupAttribution:
    name: str
    total_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    cost_basis: float
    contribution_to_return: float | None
    position_count: int
    win_count: int
    loss_count: int

    @property
    def win_rate(self) -> float | None:
        decided = self.win_count + self.loss_count
        return self.win_count / decided if decided else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total_pnl": round(self.total_pnl, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "cost_basis": round(self.cost_basis, 2),
            "contribution_to_return": (
                None if self.contribution_to_return is None
                else round(self.contribution_to_return, 5)
            ),
            "position_count": self.position_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": None if self.win_rate is None else round(self.win_rate, 3),
        }


@dataclass
class AttributionReport:
    as_of: date
    portfolio_value: float
    initial_capital: float
    total_return: float | None
    benchmark_return: float | None
    alpha: float | None
    period_returns: dict[str, float | None] = field(default_factory=dict)
    by_position: list[PositionContribution] = field(default_factory=list)
    by_strategy: list[GroupAttribution] = field(default_factory=list)
    by_sector: list[GroupAttribution] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def best_position(self) -> PositionContribution | None:
        return max(self.by_position, key=lambda p: p.total_pnl, default=None)

    @property
    def worst_position(self) -> PositionContribution | None:
        return min(self.by_position, key=lambda p: p.total_pnl, default=None)

    @property
    def best_strategy(self) -> GroupAttribution | None:
        return max(self.by_strategy, key=lambda g: g.total_pnl, default=None)

    @property
    def worst_strategy(self) -> GroupAttribution | None:
        return min(self.by_strategy, key=lambda g: g.total_pnl, default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "portfolio_value": round(self.portfolio_value, 2),
            "initial_capital": self.initial_capital,
            "total_return": None if self.total_return is None else round(self.total_return, 5),
            "benchmark_return": (
                None if self.benchmark_return is None else round(self.benchmark_return, 5)
            ),
            "alpha": None if self.alpha is None else round(self.alpha, 5),
            "period_returns": {
                k: (None if v is None else round(v, 5)) for k, v in self.period_returns.items()
            },
            "by_position": [p.to_dict() for p in self.by_position],
            "by_strategy": [g.to_dict() for g in self.by_strategy],
            "by_sector": [g.to_dict() for g in self.by_sector],
            "best_position": self.best_position.to_dict() if self.best_position else None,
            "worst_position": self.worst_position.to_dict() if self.worst_position else None,
            "best_strategy": self.best_strategy.to_dict() if self.best_strategy else None,
            "worst_strategy": self.worst_strategy.to_dict() if self.worst_strategy else None,
            "notes": self.notes,
        }


def _price_at(session: Session, ticker: str, as_of: date) -> float | None:
    bar = session.scalar(
        select(PriceBar)
        .where(PriceBar.ticker == ticker.upper(), PriceBar.timestamp <= as_of)
        .order_by(PriceBar.timestamp.desc())
    )
    if bar is None:
        return None
    return bar.close if bar.close is not None else bar.adjusted_close


def period_return(
    session: Session, portfolio: Portfolio, *, as_of: date, days: int
) -> float | None:
    """Return over the trailing *days*, from stored snapshots."""
    end = session.scalar(
        select(PortfolioSnapshot)
        .where(
            PortfolioSnapshot.portfolio_id == portfolio.portfolio_id,
            PortfolioSnapshot.as_of <= as_of,
        )
        .order_by(PortfolioSnapshot.as_of.desc())
    )
    if end is None:
        return None
    start = session.scalar(
        select(PortfolioSnapshot)
        .where(
            PortfolioSnapshot.portfolio_id == portfolio.portfolio_id,
            PortfolioSnapshot.as_of <= as_of - timedelta(days=days),
        )
        .order_by(PortfolioSnapshot.as_of.desc())
    )
    if start is None or not start.total_value:
        return None
    return (end.total_value - start.total_value) / start.total_value


def benchmark_period_return(
    session: Session, ticker: str, *, as_of: date, days: int
) -> float | None:
    end_price = _price_at(session, ticker, as_of)
    start_price = _price_at(session, ticker, as_of - timedelta(days=days))
    if not end_price or not start_price:
        return None
    return (end_price - start_price) / start_price


def analyze_attribution(
    session: Session, portfolio: Portfolio, *, as_of: date | None = None
) -> AttributionReport:
    """Attribute portfolio performance to positions, strategies and sectors."""
    from backend.portfolio.paper_trading import mark_to_market

    as_of = as_of or date.today()
    state = mark_to_market(session, portfolio, as_of=as_of)
    total_value = state["total_value"]
    initial = portfolio.initial_capital

    report = AttributionReport(
        as_of=as_of,
        portfolio_value=total_value,
        initial_capital=initial,
        total_return=safe_or_none(safe_div(total_value - initial, initial)),
        benchmark_return=None,
        alpha=None,
    )

    # --- Aggregate the trade ledger per (ticker, direction) -----------------
    trades = list(session.execute(
        select(Trade).where(
            Trade.portfolio_id == portfolio.portfolio_id,
            Trade.executed_at <= datetime(as_of.year, as_of.month, as_of.day, 23, 59, tzinfo=timezone.utc).replace(tzinfo=None),
        ).order_by(Trade.executed_at)
    ).scalars().all())

    ledger: dict[tuple[str, str], dict[str, Any]] = {}
    for trade in trades:
        key = (trade.ticker, trade.direction)
        entry = ledger.setdefault(key, {
            "realized": 0.0, "cost_basis": 0.0, "strategy": trade.strategy,
            "opened": trade.executed_at,
        })
        if trade.side in ("BUY", "SHORT"):
            entry["cost_basis"] += trade.gross_value
        if trade.realized_pnl is not None:
            entry["realized"] += trade.realized_pnl
        if trade.strategy:
            entry["strategy"] = trade.strategy

    open_positions = {
        (p.ticker, p.direction): p for p in state["positions"]
    }

    contributions: list[PositionContribution] = []
    for (ticker, direction), entry in ledger.items():
        position = open_positions.get((ticker, direction))
        unrealized = (position.unrealized_pnl or 0.0) if position else 0.0
        realized = entry["realized"]
        total_pnl = realized + unrealized
        cost_basis = entry["cost_basis"]
        contributions.append(PositionContribution(
            ticker=ticker,
            strategy=(position.strategy if position else None) or entry.get("strategy"),
            sector=position.sector if position else _sector_for(session, ticker),
            direction=direction,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=total_pnl,
            cost_basis=cost_basis,
            return_on_cost=safe_or_none(safe_div(total_pnl, cost_basis)),
            contribution_to_return=safe_or_none(safe_div(total_pnl, initial)),
            is_open=position is not None,
        ))

    contributions.sort(key=lambda c: -c.total_pnl)
    report.by_position = contributions

    if state["unpriced_tickers"]:
        report.notes.append(
            f"Unrealised P&L excludes {', '.join(state['unpriced_tickers'])} — no current "
            "price is stored for these names."
        )

    # --- Group attribution ---------------------------------------------------
    report.by_strategy = _group(contributions, lambda c: c.strategy or "unclassified", initial)
    report.by_sector = _group(contributions, lambda c: c.sector or "Unclassified", initial)

    # --- Benchmark and alpha -------------------------------------------------
    first_snapshot = session.scalar(
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.portfolio_id == portfolio.portfolio_id)
        .order_by(PortfolioSnapshot.as_of)
    )
    inception = first_snapshot.as_of if first_snapshot else (
        trades[0].executed_at.date() if trades else as_of
    )
    start_benchmark = _price_at(session, portfolio.benchmark_ticker, inception)
    end_benchmark = _price_at(session, portfolio.benchmark_ticker, as_of)
    if start_benchmark and end_benchmark:
        report.benchmark_return = (end_benchmark - start_benchmark) / start_benchmark
        if report.total_return is not None:
            report.alpha = report.total_return - report.benchmark_return
    else:
        report.notes.append(
            f"Benchmark return unavailable: no stored prices for "
            f"{portfolio.benchmark_ticker} across the period."
        )

    # --- Period returns ------------------------------------------------------
    for label, days in (("daily", 1), ("weekly", 7), ("monthly", 30), ("ytd", _ytd_days(as_of))):
        report.period_returns[label] = period_return(session, portfolio, as_of=as_of, days=days)
        report.period_returns[f"benchmark_{label}"] = benchmark_period_return(
            session, portfolio.benchmark_ticker, as_of=as_of, days=days
        )
    return report


def _ytd_days(as_of: date) -> int:
    return max(1, (as_of - date(as_of.year, 1, 1)).days)


def _sector_for(session: Session, ticker: str) -> str | None:
    from backend.data.models import Company

    company = session.scalar(select(Company).where(Company.ticker == ticker.upper()))
    return company.sector if company else None


def _group(
    contributions: Sequence[PositionContribution],
    key_fn,
    initial_capital: float,
) -> list[GroupAttribution]:
    buckets: dict[str, list[PositionContribution]] = {}
    for contribution in contributions:
        buckets.setdefault(key_fn(contribution), []).append(contribution)

    out: list[GroupAttribution] = []
    for name, items in buckets.items():
        total_pnl = sum(i.total_pnl for i in items)
        out.append(GroupAttribution(
            name=name,
            total_pnl=total_pnl,
            realized_pnl=sum(i.realized_pnl for i in items),
            unrealized_pnl=sum(i.unrealized_pnl for i in items),
            cost_basis=sum(i.cost_basis for i in items),
            contribution_to_return=safe_or_none(safe_div(total_pnl, initial_capital)),
            position_count=len(items),
            win_count=sum(1 for i in items if i.total_pnl > 0),
            loss_count=sum(1 for i in items if i.total_pnl < 0),
        ))
    out.sort(key=lambda g: -g.total_pnl)
    return out


def safe_or_none(value: Any) -> float | None:
    from backend.core.data_quality import is_available

    return float(value) if is_available(value) else None
