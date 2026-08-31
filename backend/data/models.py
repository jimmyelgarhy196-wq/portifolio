"""SQLAlchemy ORM models for GMG Investment Intelligence.

Provenance convention
---------------------
Every table holding ingested or derived data carries the four data-quality
columns ``source``, ``retrieved_at``, ``data_period``, ``confidence`` via the
:class:`ProvenanceMixin`. This is what makes "every numerical claim comes from
a source" enforceable at the storage layer rather than by convention.

Append-only tables
------------------
``recommendations``, ``score_history``, ``thesis_versions`` and ``reports`` are
never updated in place. They are the system's memory, and grading its own past
predictions depends on that history being immutable.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ProvenanceMixin:
    """Data-quality columns required on every ingested/derived record."""

    source: Mapped[str] = mapped_column(String(64), nullable=False, default="UNKNOWN")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    data_period: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="HIGH")


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
class Company(Base):
    __tablename__ = "companies"

    company_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(24), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_ar: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(96), index=True)
    industry: Mapped[str | None] = mapped_column(String(128))
    exchange: Mapped[str] = mapped_column(String(16), default="EGX", nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="EGP", nullable=False)
    listing_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", nullable=False)

    # Index membership drives the investable universe.
    in_egx30: Mapped[bool] = mapped_column(Boolean, default=False)
    in_egx70: Mapped[bool] = mapped_column(Boolean, default=False)
    in_egx100: Mapped[bool] = mapped_column(Boolean, default=False)

    # Provider-specific symbol mapping (e.g. {"yahoo": "COMI.CA"}).
    provider_symbols: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    shares_outstanding: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    prices: Mapped[list["PriceBar"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "ticker": self.ticker,
            "name": self.name,
            "sector": self.sector,
            "industry": self.industry,
            "exchange": self.exchange,
            "currency": self.currency,
            "listing_date": self.listing_date.isoformat() if self.listing_date else None,
            "status": self.status,
            "in_egx30": self.in_egx30,
            "in_egx70": self.in_egx70,
            "in_egx100": self.in_egx100,
            "shares_outstanding": self.shares_outstanding,
        }


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
class PriceBar(Base, ProvenanceMixin):
    __tablename__ = "price_history"
    __table_args__ = (
        # Prevents duplicate bars — the primary defence against double ingestion.
        UniqueConstraint("ticker", "timestamp", name="uq_price_ticker_ts"),
        Index("ix_price_ticker_ts", "ticker", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.company_id", ondelete="CASCADE"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    timestamp: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    adjusted_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)

    company: Mapped["Company | None"] = relationship(back_populates="prices")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "adjusted_close": self.adjusted_close,
            "volume": self.volume,
            "source": self.source,
        }


class FinancialStatement(Base, ProvenanceMixin):
    __tablename__ = "financial_statements"
    __table_args__ = (
        UniqueConstraint("ticker", "period", "period_type", name="uq_fin_ticker_period"),
        Index("ix_fin_ticker_period", "ticker", "period_end"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(24), nullable=False)   # e.g. "2024-Q3", "2024-FY"
    period_type: Mapped[str] = mapped_column(String(12), nullable=False)  # Q | FY | H
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    #: When this statement actually became public. Backtests use this — not
    #: ``period_end`` — so a result is invisible until it was really published.
    available_from: Mapped[date | None] = mapped_column(Date, index=True)

    revenue: Mapped[float | None] = mapped_column(Float)
    gross_profit: Mapped[float | None] = mapped_column(Float)
    ebitda: Mapped[float | None] = mapped_column(Float)
    operating_income: Mapped[float | None] = mapped_column(Float)
    net_income: Mapped[float | None] = mapped_column(Float)
    eps: Mapped[float | None] = mapped_column(Float)
    cash: Mapped[float | None] = mapped_column(Float)
    total_debt: Mapped[float | None] = mapped_column(Float)
    total_assets: Mapped[float | None] = mapped_column(Float)
    total_equity: Mapped[float | None] = mapped_column(Float)
    operating_cash_flow: Mapped[float | None] = mapped_column(Float)
    capex: Mapped[float | None] = mapped_column(Float)
    free_cash_flow: Mapped[float | None] = mapped_column(Float)
    interest_expense: Mapped[float | None] = mapped_column(Float)
    current_assets: Mapped[float | None] = mapped_column(Float)
    current_liabilities: Mapped[float | None] = mapped_column(Float)
    dividends_paid: Mapped[float | None] = mapped_column(Float)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "period": self.period,
            "period_type": self.period_type,
            "period_end": self.period_end.isoformat(),
            "available_from": self.available_from.isoformat() if self.available_from else None,
            "revenue": self.revenue,
            "gross_profit": self.gross_profit,
            "ebitda": self.ebitda,
            "operating_income": self.operating_income,
            "net_income": self.net_income,
            "eps": self.eps,
            "cash": self.cash,
            "total_debt": self.total_debt,
            "total_assets": self.total_assets,
            "total_equity": self.total_equity,
            "operating_cash_flow": self.operating_cash_flow,
            "capex": self.capex,
            "free_cash_flow": self.free_cash_flow,
            "source": self.source,
        }


class ValuationSnapshot(Base, ProvenanceMixin):
    __tablename__ = "valuation_snapshots"
    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_val_ticker_date"),
        Index("ix_val_ticker_date", "ticker", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    market_cap: Mapped[float | None] = mapped_column(Float)
    enterprise_value: Mapped[float | None] = mapped_column(Float)
    pe: Mapped[float | None] = mapped_column(Float)
    pb: Mapped[float | None] = mapped_column(Float)
    ps: Mapped[float | None] = mapped_column(Float)
    ev_ebitda: Mapped[float | None] = mapped_column(Float)
    ev_sales: Mapped[float | None] = mapped_column(Float)
    fcf_yield: Mapped[float | None] = mapped_column(Float)
    dividend_yield: Mapped[float | None] = mapped_column(Float)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "date": self.date.isoformat(),
            "market_cap": self.market_cap,
            "pe": self.pe,
            "pb": self.pb,
            "ps": self.ps,
            "ev_ebitda": self.ev_ebitda,
            "ev_sales": self.ev_sales,
            "fcf_yield": self.fcf_yield,
            "dividend_yield": self.dividend_yield,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# News and disclosures
# ---------------------------------------------------------------------------
class NewsItem(Base, ProvenanceMixin):
    __tablename__ = "news"
    __table_args__ = (
        UniqueConstraint("url_hash", name="uq_news_url_hash"),
        Index("ix_news_ticker_date", "ticker", "publication_date"),
    )

    news_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str | None] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    news_source: Mapped[str | None] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_date: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    #: -1.0 .. +1.0, or NULL when no sentiment could be derived.
    sentiment: Mapped[float | None] = mapped_column(Float)
    sentiment_label: Mapped[str | None] = mapped_column(String(16))
    importance: Mapped[int | None] = mapped_column(Integer)  # 1..5

    def to_dict(self) -> dict[str, Any]:
        return {
            "news_id": self.news_id,
            "ticker": self.ticker,
            "title": self.title,
            "source": self.news_source or self.source,
            "url": self.url,
            "publication_date": (
                self.publication_date.isoformat() if self.publication_date else None
            ),
            "summary": self.summary,
            "sentiment": self.sentiment,
            "sentiment_label": self.sentiment_label,
            "importance": self.importance,
        }


class Disclosure(Base, ProvenanceMixin):
    __tablename__ = "disclosures"
    __table_args__ = (
        UniqueConstraint("url_hash", name="uq_disclosure_url_hash"),
        Index("ix_disclosure_ticker_date", "ticker", "date"),
    )

    disclosure_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str | None] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    date: Mapped[date | None] = mapped_column(Date, index=True)
    disclosure_type: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    importance: Mapped[int | None] = mapped_column(Integer)

    def to_dict(self) -> dict[str, Any]:
        return {
            "disclosure_id": self.disclosure_id,
            "ticker": self.ticker,
            "title": self.title,
            "date": self.date.isoformat() if self.date else None,
            "type": self.disclosure_type,
            "source": self.source,
            "url": self.url,
            "summary": self.summary,
            "importance": self.importance,
        }


# ---------------------------------------------------------------------------
# Research: theses, recommendations, score history (the system's memory)
# ---------------------------------------------------------------------------
class ResearchThesis(Base):
    """A durable investment thesis. Updated weekly rather than recreated."""

    __tablename__ = "research_theses"
    __table_args__ = (Index("ix_thesis_ticker_status", "ticker", "status"),)

    thesis_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Human-facing reference, e.g. "EGX-00047".
    reference: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(12), nullable=False)   # LONG | SHORT
    strategy: Mapped[str] = mapped_column(String(48), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    entry_price: Mapped[float | None] = mapped_column(Float)
    target_price: Mapped[float | None] = mapped_column(Float)
    invalidation_price: Mapped[float | None] = mapped_column(Float)
    expected_return: Mapped[float | None] = mapped_column(Float)
    expected_downside: Mapped[float | None] = mapped_column(Float)
    risk_reward: Mapped[float | None] = mapped_column(Float)
    expected_holding_period: Mapped[str | None] = mapped_column(String(48))
    conviction: Mapped[float | None] = mapped_column(Float)  # 0..10

    fundamental_score: Mapped[float | None] = mapped_column(Float)
    technical_score: Mapped[float | None] = mapped_column(Float)
    quant_score: Mapped[float | None] = mapped_column(Float)
    catalyst_score: Mapped[float | None] = mapped_column(Float)
    risk_score: Mapped[float | None] = mapped_column(Float)
    alpha_score: Mapped[float | None] = mapped_column(Float)

    thesis_text: Mapped[str | None] = mapped_column(Text)
    bull_case: Mapped[str | None] = mapped_column(Text)
    bear_case: Mapped[str | None] = mapped_column(Text)
    catalysts: Mapped[list[Any]] = mapped_column(JSON, default=list)
    risks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    invalidation_conditions: Mapped[list[Any]] = mapped_column(JSON, default=list)
    data_sources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    #: Tagged FACT/CALCULATION/INFERENCE/OPINION/UNKNOWN statements.
    statements: Mapped[list[Any]] = mapped_column(JSON, default=list)

    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", nullable=False)
    generated_by: Mapped[str] = mapped_column(String(32), default="deterministic")
    version: Mapped[int] = mapped_column(Integer, default=1)

    versions: Mapped[list["ThesisVersion"]] = relationship(
        back_populates="thesis", cascade="all, delete-orphan",
        order_by="ThesisVersion.version.desc()",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis_id": self.thesis_id,
            "reference": self.reference,
            "ticker": self.ticker,
            "direction": self.direction,
            "strategy": self.strategy,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "entry_price": self.entry_price,
            "target_price": self.target_price,
            "invalidation_price": self.invalidation_price,
            "expected_return": self.expected_return,
            "expected_downside": self.expected_downside,
            "risk_reward": self.risk_reward,
            "expected_holding_period": self.expected_holding_period,
            "conviction": self.conviction,
            "fundamental_score": self.fundamental_score,
            "technical_score": self.technical_score,
            "quant_score": self.quant_score,
            "catalyst_score": self.catalyst_score,
            "risk_score": self.risk_score,
            "alpha_score": self.alpha_score,
            "thesis_text": self.thesis_text,
            "bull_case": self.bull_case,
            "bear_case": self.bear_case,
            "catalysts": self.catalysts or [],
            "risks": self.risks or [],
            "invalidation_conditions": self.invalidation_conditions or [],
            "data_sources": self.data_sources or [],
            "statements": self.statements or [],
            "status": self.status,
            "generated_by": self.generated_by,
            "version": self.version,
        }


class ThesisVersion(Base):
    """Immutable weekly snapshot of a thesis, enabling 'what changed' diffs."""

    __tablename__ = "thesis_versions"
    __table_args__ = (
        UniqueConstraint("thesis_id", "version", name="uq_thesis_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thesis_id: Mapped[int] = mapped_column(
        ForeignKey("research_theses.thesis_id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    change_summary: Mapped[str | None] = mapped_column(Text)

    thesis: Mapped["ResearchThesis"] = relationship(back_populates="versions")


class Recommendation(Base):
    """Append-only record of every recommendation, for later grading."""

    __tablename__ = "recommendations"
    __table_args__ = (Index("ix_reco_ticker_date", "ticker", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    thesis_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_theses.thesis_id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(12), nullable=False)  # BUY|HOLD|SELL|WATCH
    direction: Mapped[str] = mapped_column(String(12), default="LONG")
    strategy: Mapped[str | None] = mapped_column(String(48), index=True)
    sector: Mapped[str | None] = mapped_column(String(96), index=True)

    price_at_reco: Mapped[float | None] = mapped_column(Float)
    target_price: Mapped[float | None] = mapped_column(Float)
    invalidation_price: Mapped[float | None] = mapped_column(Float)
    conviction: Mapped[float | None] = mapped_column(Float)
    alpha_score: Mapped[float | None] = mapped_column(Float)
    expected_return: Mapped[float | None] = mapped_column(Float)
    expected_holding_period: Mapped[str | None] = mapped_column(String(48))
    rationale: Mapped[str | None] = mapped_column(Text)

    # --- outcome, filled in later by the evaluation engine -------------------
    outcome_status: Mapped[str] = mapped_column(String(24), default="OPEN")
    outcome_price: Mapped[float | None] = mapped_column(Float)
    outcome_date: Mapped[datetime | None] = mapped_column(DateTime)
    realized_return: Mapped[float | None] = mapped_column(Float)
    benchmark_return: Mapped[float | None] = mapped_column(Float)
    holding_days: Mapped[int | None] = mapped_column(Integer)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "thesis_id": self.thesis_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "action": self.action,
            "direction": self.direction,
            "strategy": self.strategy,
            "sector": self.sector,
            "price_at_reco": self.price_at_reco,
            "target_price": self.target_price,
            "conviction": self.conviction,
            "alpha_score": self.alpha_score,
            "expected_return": self.expected_return,
            "outcome_status": self.outcome_status,
            "realized_return": self.realized_return,
            "benchmark_return": self.benchmark_return,
            "holding_days": self.holding_days,
        }


class ScoreHistory(Base):
    """Append-only score record, powering week-over-week score deltas."""

    __tablename__ = "score_history"
    __table_args__ = (
        UniqueConstraint("ticker", "as_of", name="uq_score_ticker_date"),
        Index("ix_score_ticker_date", "ticker", "as_of"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    alpha_score: Mapped[float | None] = mapped_column(Float)
    fundamental_score: Mapped[float | None] = mapped_column(Float)
    technical_score: Mapped[float | None] = mapped_column(Float)
    quant_score: Mapped[float | None] = mapped_column(Float)
    catalyst_score: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)
    risk_score: Mapped[float | None] = mapped_column(Float)
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[str | None] = mapped_column(String(16))
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
class Portfolio(Base):
    __tablename__ = "portfolios"

    portfolio_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    #: PAPER only in this system. Present so a future live mode is explicit.
    mode: Mapped[str] = mapped_column(String(16), default="PAPER", nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="EGP")
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    benchmark_ticker: Mapped[str] = mapped_column(String(24), default="EGX30")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    positions: Mapped[list["Position"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )
    trades: Mapped[list["Trade"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "ticker", "direction", name="uq_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.portfolio_id", ondelete="CASCADE"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(12), default="LONG", nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    average_price: Mapped[float] = mapped_column(Float, default=0.0)
    current_price: Mapped[float | None] = mapped_column(Float)
    market_value: Mapped[float | None] = mapped_column(Float)
    portfolio_weight: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    strategy: Mapped[str | None] = mapped_column(String(48), index=True)
    sector: Mapped[str | None] = mapped_column(String(96))
    thesis_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_theses.thesis_id", ondelete="SET NULL")
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    portfolio: Mapped["Portfolio"] = relationship(back_populates="positions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "direction": self.direction,
            "quantity": self.quantity,
            "average_price": self.average_price,
            "current_price": self.current_price,
            "market_value": self.market_value,
            "portfolio_weight": self.portfolio_weight,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "strategy": self.strategy,
            "sector": self.sector,
            "thesis_id": self.thesis_id,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
        }


class Trade(Base):
    """Immutable paper-trade ledger entry."""

    __tablename__ = "trades"
    __table_args__ = (Index("ix_trade_portfolio_date", "portfolio_id", "executed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.portfolio_id", ondelete="CASCADE"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(12), nullable=False)  # BUY|SELL|SHORT|COVER
    direction: Mapped[str] = mapped_column(String(12), default="LONG")
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    gross_value: Mapped[float] = mapped_column(Float, default=0.0)
    net_value: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    strategy: Mapped[str | None] = mapped_column(String(48))
    thesis_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    #: Always "PAPER" in this system — the ledger records its own nature.
    mode: Mapped[str] = mapped_column(String(16), default="PAPER", nullable=False)

    portfolio: Mapped["Portfolio"] = relationship(back_populates="trades")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "side": self.side,
            "direction": self.direction,
            "quantity": self.quantity,
            "price": self.price,
            "commission": self.commission,
            "slippage": self.slippage,
            "net_value": self.net_value,
            "realized_pnl": self.realized_pnl,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "strategy": self.strategy,
            "mode": self.mode,
        }


class PortfolioSnapshot(Base):
    """Daily portfolio valuation, used for return series and drawdown."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "as_of", name="uq_snapshot_portfolio_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.portfolio_id", ondelete="CASCADE"), index=True
    )
    as_of: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    total_value: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    invested_value: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    benchmark_value: Mapped[float | None] = mapped_column(Float)


# ---------------------------------------------------------------------------
# Watchlist, alerts, reports, backtests, settings, data-quality log
# ---------------------------------------------------------------------------
class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("list_name", "ticker", name="uq_watchlist_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: core | swing | short | special_situations
    list_name: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text)
    target_price: Mapped[float | None] = mapped_column(Float)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "list_name": self.list_name,
            "ticker": self.ticker,
            "note": self.note,
            "target_price": self.target_price,
            "added_at": self.added_at.isoformat() if self.added_at else None,
        }


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_created", "created_at", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str | None] = mapped_column(String(24), index=True)
    alert_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info|warning|critical
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="NEW")  # NEW|ACK|DISMISSED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "payload": self.payload or {},
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("ix_report_period", "report_type", "period_end"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_type: Mapped[str] = mapped_column(String(48), default="weekly_committee")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    markdown: Mapped[str | None] = mapped_column(Text)
    #: Structured sections, so reports can be compared programmatically.
    sections: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generated_by: Mapped[str] = mapped_column(String(32), default="deterministic")
    contains_synthetic_data: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "report_type": self.report_type,
            "title": self.title,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "generated_by": self.generated_by,
            "contains_synthetic_data": self.contains_synthetic_data,
        }


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    equity_curve: Mapped[list[Any]] = mapped_column(JSON, default=list)
    trades: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    contains_synthetic_data: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "strategy": self.strategy,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "initial_capital": self.initial_capital,
            "parameters": self.parameters or {},
            "metrics": self.metrics or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "contains_synthetic_data": self.contains_synthetic_data,
        }


class SettingRecord(Base):
    """User-editable settings overriding YAML/env defaults."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DataQualityLog(Base):
    """Audit trail of every ingestion run: what was fetched, from where, failures."""

    __tablename__ = "data_quality_log"
    __table_args__ = (Index("ix_dq_dataset_time", "dataset", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(48), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(24), index=True)
    provider: Mapped[str | None] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(24), default="OK")  # OK|PARTIAL|FAILED|SKIPPED
    rows_ingested: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset": self.dataset,
            "ticker": self.ticker,
            "provider": self.provider,
            "status": self.status,
            "rows_ingested": self.rows_ingested,
            "rows_skipped": self.rows_skipped,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
