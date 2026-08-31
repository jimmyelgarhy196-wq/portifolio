"""Paper trading engine.

Simulated execution only. There is no broker integration anywhere in this
codebase, and :func:`assert_paper_only` guards every write path so that stays
true even if a live mode is added later.

Fills are modelled with commission and slippage from ``config/risk.yaml``, so
simulated performance is not flattered by frictionless execution. Short
positions are supported and always labelled PAPER SHORT.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.config import get_settings, load_yaml_config
from backend.core.logging_config import EVENT_PORTFOLIO_CHANGE, get_logger, log_event
from backend.data.models import (
    Company,
    Portfolio,
    PortfolioSnapshot,
    Position,
    PriceBar,
    Trade,
)

logger = get_logger(__name__)

DEFAULT_PORTFOLIO_NAME = "GMG Research Portfolio"


class TradeRejected(Exception):
    """A trade could not be executed (insufficient cash, no price, bad input)."""


class LiveTradingBlocked(Exception):
    """Raised if anything attempts to execute outside paper mode."""


def assert_paper_only(portfolio: Portfolio) -> None:
    """Hard gate. This system simulates; it does not trade.

    ``EGX_LIVE_TRADING_ENABLED`` exists so a future execution module has one
    auditable switch — but no code path here honours it, by design.
    """
    if portfolio.mode != "PAPER":
        raise LiveTradingBlocked(
            f"Portfolio {portfolio.name!r} is in mode {portfolio.mode!r}. This system "
            "executes paper trades only and has no broker integration."
        )
    if get_settings().live_trading_enabled:
        raise LiveTradingBlocked(
            "EGX_LIVE_TRADING_ENABLED is set, but no live execution path exists in "
            "this codebase. Unset it to continue in paper mode."
        )


@dataclass
class FillModel:
    """Transaction cost assumptions applied to every simulated fill."""

    commission_bps: float = 20.0
    slippage_bps: float = 15.0

    @classmethod
    def from_config(cls) -> "FillModel":
        costs = load_yaml_config("risk").get("costs", {})
        settings = get_settings()
        return cls(
            commission_bps=float(costs.get("commission_bps", settings.commission_bps)),
            slippage_bps=float(costs.get("slippage_bps", settings.slippage_bps)),
        )

    def fill_price(self, reference_price: float, side: str) -> float:
        """Slippage always works against the trader."""
        drift = self.slippage_bps / 10_000.0
        if side.upper() in ("BUY", "COVER"):
            return reference_price * (1.0 + drift)
        return reference_price * (1.0 - drift)

    def commission(self, gross_value: float) -> float:
        return abs(gross_value) * (self.commission_bps / 10_000.0)


def get_or_create_portfolio(
    session: Session,
    name: str = DEFAULT_PORTFOLIO_NAME,
    *,
    initial_capital: float | None = None,
) -> Portfolio:
    portfolio = session.scalar(select(Portfolio).where(Portfolio.name == name))
    if portfolio:
        return portfolio
    settings = get_settings()
    capital = initial_capital if initial_capital is not None else settings.portfolio_capital
    portfolio = Portfolio(
        name=name,
        mode="PAPER",
        currency=settings.portfolio_currency,
        initial_capital=capital,
        cash=capital,
        benchmark_ticker=settings.benchmark_ticker,
        settings={},
    )
    session.add(portfolio)
    session.flush()
    log_event(
        logger, EVENT_PORTFOLIO_CHANGE,
        f"Created paper portfolio {name!r} with {settings.portfolio_currency} {capital:,.0f}",
        portfolio=name, capital=capital,
    )
    return portfolio


def latest_price(session: Session, ticker: str, *, as_of: date | None = None) -> float | None:
    stmt = select(PriceBar).where(PriceBar.ticker == ticker.upper())
    if as_of:
        stmt = stmt.where(PriceBar.timestamp <= as_of)
    bar = session.scalar(stmt.order_by(PriceBar.timestamp.desc()))
    if bar is None:
        return None
    return bar.close if bar.close is not None else bar.adjusted_close


def execute_trade(
    session: Session,
    portfolio: Portfolio,
    *,
    ticker: str,
    side: str,
    quantity: float,
    price: float | None = None,
    as_of: date | None = None,
    strategy: str | None = None,
    thesis_id: int | None = None,
    note: str | None = None,
    fill_model: FillModel | None = None,
) -> Trade:
    """Execute one simulated trade and update cash and positions.

    ``side`` is BUY | SELL | SHORT | COVER. Prices default to the latest stored
    bar; a trade cannot execute without a real price, because inventing one
    would be fabricating a market.
    """
    assert_paper_only(portfolio)
    side = side.upper()
    ticker = ticker.upper()
    if side not in ("BUY", "SELL", "SHORT", "COVER"):
        raise TradeRejected(f"Unknown side {side!r}")
    if quantity <= 0:
        raise TradeRejected("Quantity must be positive.")

    fill_model = fill_model or FillModel.from_config()
    reference = price if price is not None else latest_price(session, ticker, as_of=as_of)
    if reference is None or reference <= 0:
        raise TradeRejected(
            f"No price available for {ticker}. A trade cannot be simulated without a "
            "real price — ingest market data first."
        )

    fill = fill_model.fill_price(reference, side)
    gross = fill * quantity
    commission = fill_model.commission(gross)
    slippage_cost = abs(fill - reference) * quantity

    direction = "SHORT" if side in ("SHORT", "COVER") else "LONG"
    position = session.scalar(
        select(Position).where(
            Position.portfolio_id == portfolio.portfolio_id,
            Position.ticker == ticker,
            Position.direction == direction,
        )
    )
    company = session.scalar(select(Company).where(Company.ticker == ticker))
    realized = None

    if side == "BUY":
        cost = gross + commission
        if cost > portfolio.cash + 1e-6:
            raise TradeRejected(
                f"Insufficient cash: trade costs {cost:,.2f} but only "
                f"{portfolio.cash:,.2f} is available."
            )
        portfolio.cash -= cost
        if position is None:
            position = Position(
                portfolio_id=portfolio.portfolio_id, ticker=ticker, direction="LONG",
                quantity=quantity, average_price=fill, strategy=strategy,
                sector=company.sector if company else None, thesis_id=thesis_id,
            )
            session.add(position)
        else:
            total_cost = position.average_price * position.quantity + gross
            position.quantity += quantity
            position.average_price = total_cost / position.quantity
            if strategy:
                position.strategy = strategy
            if thesis_id:
                position.thesis_id = thesis_id

    elif side == "SELL":
        if position is None or position.quantity < quantity - 1e-9:
            held = position.quantity if position else 0
            raise TradeRejected(
                f"Cannot sell {quantity:g} {ticker}: only {held:g} held."
            )
        realized = (fill - position.average_price) * quantity - commission
        portfolio.cash += gross - commission
        position.quantity -= quantity
        position.realized_pnl = (position.realized_pnl or 0.0) + realized
        if position.quantity <= 1e-9:
            session.delete(position)
            position = None

    elif side == "SHORT":
        # Paper short: proceeds are credited, and the obligation is tracked as a
        # negative-quantity position. No borrow is modelled — this is research.
        portfolio.cash += gross - commission
        if position is None:
            position = Position(
                portfolio_id=portfolio.portfolio_id, ticker=ticker, direction="SHORT",
                quantity=quantity, average_price=fill, strategy=strategy,
                sector=company.sector if company else None, thesis_id=thesis_id,
            )
            session.add(position)
        else:
            total = position.average_price * position.quantity + gross
            position.quantity += quantity
            position.average_price = total / position.quantity

    else:  # COVER
        if position is None or position.quantity < quantity - 1e-9:
            held = position.quantity if position else 0
            raise TradeRejected(
                f"Cannot cover {quantity:g} {ticker}: paper short position is {held:g}."
            )
        cost = gross + commission
        if cost > portfolio.cash + 1e-6:
            raise TradeRejected(
                f"Insufficient cash to cover: needs {cost:,.2f}, have {portfolio.cash:,.2f}."
            )
        realized = (position.average_price - fill) * quantity - commission
        portfolio.cash -= cost
        position.quantity -= quantity
        position.realized_pnl = (position.realized_pnl or 0.0) + realized
        if position.quantity <= 1e-9:
            session.delete(position)
            position = None

    trade = Trade(
        portfolio_id=portfolio.portfolio_id, ticker=ticker, side=side, direction=direction,
        quantity=quantity, price=fill, commission=commission, slippage=slippage_cost,
        gross_value=gross, net_value=gross + commission if side in ("BUY", "COVER") else gross - commission,
        realized_pnl=realized,
        executed_at=(
            datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)
            if as_of else datetime.now(timezone.utc)
        ),
        strategy=strategy, thesis_id=thesis_id, note=note, mode="PAPER",
    )
    session.add(trade)
    session.flush()

    log_event(
        logger, EVENT_PORTFOLIO_CHANGE,
        f"PAPER {side} {quantity:g} {ticker} @ {fill:,.4f}",
        portfolio=portfolio.name, ticker=ticker, side=side, quantity=quantity,
        price=round(fill, 4), commission=round(commission, 2),
        realized_pnl=None if realized is None else round(realized, 2),
    )
    return trade


def mark_to_market(
    session: Session, portfolio: Portfolio, *, as_of: date | None = None
) -> dict[str, Any]:
    """Revalue all positions and return the portfolio's current state."""
    as_of = as_of or date.today()
    positions = list(session.execute(
        select(Position).where(Position.portfolio_id == portfolio.portfolio_id)
    ).scalars().all())

    long_value = 0.0
    short_exposure = 0.0
    unrealized_total = 0.0
    priced: list[Position] = []
    unpriced: list[str] = []

    for position in positions:
        price = latest_price(session, position.ticker, as_of=as_of)
        if price is None:
            unpriced.append(position.ticker)
            position.current_price = None
            position.market_value = None
            position.unrealized_pnl = None
            continue
        position.current_price = price
        if position.direction == "SHORT":
            # Short P&L moves inversely; the liability is what must be repurchased.
            position.market_value = -price * position.quantity
            position.unrealized_pnl = (position.average_price - price) * position.quantity
            short_exposure += price * position.quantity
        else:
            position.market_value = price * position.quantity
            position.unrealized_pnl = (price - position.average_price) * position.quantity
            long_value += position.market_value
        unrealized_total += position.unrealized_pnl
        priced.append(position)

    total_value = portfolio.cash + long_value - short_exposure
    for position in priced:
        position.portfolio_weight = (
            abs(position.market_value) / total_value if total_value > 0 else None
        )

    realized_total = session.scalar(
        select(func.coalesce(func.sum(Trade.realized_pnl), 0.0)).where(
            Trade.portfolio_id == portfolio.portfolio_id
        )
    ) or 0.0

    session.flush()
    return {
        "as_of": as_of,
        "total_value": total_value,
        "cash": portfolio.cash,
        "cash_weight": portfolio.cash / total_value if total_value > 0 else None,
        "long_value": long_value,
        "short_exposure": short_exposure,
        "gross_exposure": long_value + short_exposure,
        "net_exposure": long_value - short_exposure,
        "invested_weight": (
            (long_value + short_exposure) / total_value if total_value > 0 else None
        ),
        "unrealized_pnl": unrealized_total,
        "realized_pnl": realized_total,
        "total_return": (
            (total_value - portfolio.initial_capital) / portfolio.initial_capital
            if portfolio.initial_capital else None
        ),
        "positions": positions,
        "unpriced_tickers": unpriced,
    }


def snapshot_portfolio(
    session: Session, portfolio: Portfolio, *, as_of: date | None = None
) -> PortfolioSnapshot:
    """Persist a daily valuation so returns and drawdown can be computed."""
    state = mark_to_market(session, portfolio, as_of=as_of)
    as_of = state["as_of"]
    benchmark_value = latest_price(session, portfolio.benchmark_ticker, as_of=as_of)

    existing = session.scalar(
        select(PortfolioSnapshot).where(
            PortfolioSnapshot.portfolio_id == portfolio.portfolio_id,
            PortfolioSnapshot.as_of == as_of,
        )
    )
    payload = {
        "total_value": state["total_value"],
        "cash": state["cash"],
        "invested_value": state["gross_exposure"],
        "unrealized_pnl": state["unrealized_pnl"],
        "realized_pnl": state["realized_pnl"],
        "benchmark_value": benchmark_value,
    }
    if existing:
        for key, value in payload.items():
            setattr(existing, key, value)
        session.flush()
        return existing
    snapshot = PortfolioSnapshot(
        portfolio_id=portfolio.portfolio_id, as_of=as_of, **payload
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def backfill_snapshots(
    session: Session, portfolio: Portfolio, *, start: date, end: date, step_days: int = 1
) -> int:
    """Build the historical equity curve from stored prices.

    Only meaningful once trades exist; positions are valued at each date using
    the holdings implied by trades executed on or before that date.
    """
    trades = list(session.execute(
        select(Trade).where(Trade.portfolio_id == portfolio.portfolio_id)
        .order_by(Trade.executed_at)
    ).scalars().all())
    if not trades:
        return 0

    made = 0
    current = start
    while current <= end:
        holdings: dict[tuple[str, str], list[float]] = {}
        cash = portfolio.initial_capital
        for trade in trades:
            if trade.executed_at.date() > current:
                continue
            key = (trade.ticker, trade.direction)
            qty, avg = holdings.get(key, [0.0, 0.0])
            if trade.side == "BUY":
                cash -= trade.gross_value + trade.commission
                total = avg * qty + trade.gross_value
                qty += trade.quantity
                avg = total / qty if qty else 0.0
            elif trade.side == "SELL":
                cash += trade.gross_value - trade.commission
                qty -= trade.quantity
            elif trade.side == "SHORT":
                cash += trade.gross_value - trade.commission
                total = avg * qty + trade.gross_value
                qty += trade.quantity
                avg = total / qty if qty else 0.0
            else:  # COVER
                cash -= trade.gross_value + trade.commission
                qty -= trade.quantity
            holdings[key] = [qty, avg]

        value = cash
        for (ticker, direction), (qty, _avg) in holdings.items():
            if qty <= 1e-9:
                continue
            price = latest_price(session, ticker, as_of=current)
            if price is None:
                continue
            value += price * qty if direction == "LONG" else -price * qty

        existing = session.scalar(
            select(PortfolioSnapshot).where(
                PortfolioSnapshot.portfolio_id == portfolio.portfolio_id,
                PortfolioSnapshot.as_of == current,
            )
        )
        benchmark = latest_price(session, portfolio.benchmark_ticker, as_of=current)
        if existing:
            existing.total_value = value
            existing.cash = cash
            existing.benchmark_value = benchmark
        else:
            session.add(PortfolioSnapshot(
                portfolio_id=portfolio.portfolio_id, as_of=current, total_value=value,
                cash=cash, invested_value=value - cash, unrealized_pnl=0.0,
                realized_pnl=0.0, benchmark_value=benchmark,
            ))
            made += 1
        current = date.fromordinal(current.toordinal() + step_days)

    session.flush()
    return made


def close_position(
    session: Session, portfolio: Portfolio, ticker: str, *,
    direction: str = "LONG", as_of: date | None = None, note: str | None = None,
) -> Trade | None:
    """Fully exit a position."""
    position = session.scalar(
        select(Position).where(
            Position.portfolio_id == portfolio.portfolio_id,
            Position.ticker == ticker.upper(),
            Position.direction == direction.upper(),
        )
    )
    if position is None or position.quantity <= 0:
        return None
    side = "COVER" if direction.upper() == "SHORT" else "SELL"
    return execute_trade(
        session, portfolio, ticker=ticker, side=side, quantity=position.quantity,
        as_of=as_of, strategy=position.strategy, thesis_id=position.thesis_id,
        note=note or "Position closed.",
    )
