"""Analysis service — wires the database to the analytics engines.

This is the only module in ``analytics/`` that performs I/O. The engines
themselves stay pure so they remain directly testable, and this layer is
responsible for loading rows, assembling peer groups, and persisting scores.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analytics.fundamental import (
    FinancialPeriod,
    FundamentalSnapshot,
    analyze_fundamentals,
)
from backend.analytics.master_score import (
    AlphaScore,
    CatalystEvent,
    RiskInputs,
    compute_alpha_score,
)
from backend.analytics.quant import QuantSnapshot, analyze_universe
from backend.analytics.technical import TechnicalSnapshot, analyze_technical
from backend.core.config import get_settings
from backend.core.data_quality import is_available
from backend.core.logging_config import get_logger
from backend.data.models import (
    Company,
    Disclosure,
    FinancialStatement,
    NewsItem,
    PriceBar,
    ScoreHistory,
)
from backend.data.universe import get_universe

logger = get_logger(__name__)


@dataclass
class PriceSeries:
    dates: list[date] = field(default_factory=list)
    opens: list[float | None] = field(default_factory=list)
    highs: list[float | None] = field(default_factory=list)
    lows: list[float | None] = field(default_factory=list)
    closes: list[float | None] = field(default_factory=list)
    volumes: list[float | None] = field(default_factory=list)
    retrieved_at: datetime | None = None
    source: str | None = None

    def __len__(self) -> int:
        return len(self.dates)

    @property
    def last_close(self) -> float | None:
        for value in reversed(self.closes):
            if value is not None:
                return value
        return None

    @property
    def last_date(self) -> date | None:
        return self.dates[-1] if self.dates else None


def load_price_series(
    session: Session, ticker: str, *, as_of: date | None = None, lookback_days: int = 800
) -> PriceSeries:
    """Load daily bars up to *as_of*. Never returns bars after that date."""
    as_of = as_of or date.today()
    start = as_of - timedelta(days=lookback_days)
    rows = session.execute(
        select(PriceBar)
        .where(
            PriceBar.ticker == ticker.upper(),
            PriceBar.timestamp >= start,
            PriceBar.timestamp <= as_of,
        )
        .order_by(PriceBar.timestamp)
    ).scalars().all()

    series = PriceSeries()
    for row in rows:
        series.dates.append(row.timestamp)
        series.opens.append(row.open)
        series.highs.append(row.high)
        series.lows.append(row.low)
        series.closes.append(row.adjusted_close if row.adjusted_close is not None else row.close)
        series.volumes.append(row.volume)
    if rows:
        series.retrieved_at = max(r.retrieved_at for r in rows)
        series.source = rows[-1].source
    return series


def load_financial_periods(
    session: Session,
    ticker: str,
    *,
    as_of: date | None = None,
    period_type: str = "FY",
    limit: int = 8,
    respect_availability: bool = True,
) -> list[FinancialPeriod]:
    """Load statements, newest first.

    When *respect_availability* is set (the default and the only correct choice
    for anything historical), statements are filtered by ``available_from`` —
    the date they were actually published — not by the period they cover.
    """
    stmt = select(FinancialStatement).where(
        FinancialStatement.ticker == ticker.upper(),
        FinancialStatement.period_type == period_type,
    )
    if as_of is not None and respect_availability:
        stmt = stmt.where(
            (FinancialStatement.available_from.is_(None) & (FinancialStatement.period_end <= as_of))
            | (FinancialStatement.available_from <= as_of)
        )
    rows = session.execute(
        stmt.order_by(FinancialStatement.period_end.desc()).limit(limit)
    ).scalars().all()
    return [FinancialPeriod.from_model(row) for row in rows]


def load_catalyst_events(
    session: Session, ticker: str, *, as_of: date | None = None, days: int = 180
) -> list[CatalystEvent]:
    as_of = as_of or date.today()
    since = as_of - timedelta(days=days)
    rows = session.execute(
        select(Disclosure)
        .where(
            Disclosure.ticker == ticker.upper(),
            Disclosure.date >= since,
            Disclosure.date <= as_of,
        )
        .order_by(Disclosure.date.desc())
    ).scalars().all()
    return [
        CatalystEvent(
            kind=row.disclosure_type or "OTHER",
            title=row.title,
            event_date=row.date,
            importance=row.importance or 2,
            source=row.source,
            url=row.url,
        )
        for row in rows
    ]


def load_sentiment(
    session: Session, ticker: str, *, as_of: date | None = None, days: int = 30
) -> float | None:
    """Average lexicon sentiment over recent news. ``None`` when no signal exists."""
    as_of = as_of or date.today()
    cutoff = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc) - timedelta(days=days)
    rows = session.execute(
        select(NewsItem.sentiment).where(
            NewsItem.ticker == ticker.upper(),
            NewsItem.sentiment.isnot(None),
            NewsItem.publication_date >= cutoff.replace(tzinfo=None),
        )
    ).scalars().all()
    values = [v for v in rows if v is not None]
    return sum(values) / len(values) if values else None


def _turnover(series: PriceSeries, days: int = 20) -> float | None:
    """Average daily traded value (price × volume) — the real liquidity measure."""
    pairs = [
        (c, v) for c, v in zip(series.closes[-days:], series.volumes[-days:])
        if c is not None and v is not None
    ]
    if not pairs:
        return None
    return sum(c * v for c, v in pairs) / len(pairs)


def _max_drawdown(closes: Sequence[float | None]) -> float | None:
    peak = None
    worst = None
    for value in closes:
        if value is None:
            continue
        peak = value if peak is None else max(peak, value)
        if peak > 0:
            dd = (value - peak) / peak
            worst = dd if worst is None else min(worst, dd)
    return worst


def build_universe_metrics(
    session: Session,
    tickers: Sequence[str],
    *,
    as_of: date | None = None,
) -> dict[str, dict[str, float | None]]:
    """Assemble the per-ticker metric map the factor model consumes."""
    as_of = as_of or date.today()
    out: dict[str, dict[str, float | None]] = {}

    for ticker in tickers:
        series = load_price_series(session, ticker, as_of=as_of)
        periods = load_financial_periods(session, ticker, as_of=as_of)
        company = session.scalar(select(Company).where(Company.ticker == ticker.upper()))

        metrics: dict[str, float | None] = {}
        if len(series) >= 30:
            tech = analyze_technical(
                ticker, series.dates, series.opens, series.highs,
                series.lows, series.closes, series.volumes,
            )
            metrics.update({
                "momentum_1m": tech.momentum_1m, "momentum_3m": tech.momentum_3m,
                "momentum_6m": tech.momentum_6m, "momentum_12m": tech.momentum_12m,
                "volatility_20d": tech.volatility_20d, "atr_pct": tech.atr_pct,
            })
        metrics["average_turnover"] = _turnover(series)

        if periods:
            fundamental = analyze_fundamentals(
                ticker, periods,
                price=series.last_close,
                shares_outstanding=company.shares_outstanding if company else None,
            )
            for key in (
                "pe", "pb", "ev_ebitda", "fcf_yield", "roe", "roic", "net_margin",
                "debt_to_equity", "revenue_growth", "eps_growth", "revenue_cagr",
                "market_cap",
            ):
                metric = fundamental.metrics.get(key)
                metrics[key] = metric.value if metric and metric.available else None

        out[ticker.upper()] = metrics
    return out


@dataclass
class StockAnalysis:
    """The complete analytical picture for one stock."""

    ticker: str
    company: Company | None
    as_of: date
    price_series: PriceSeries
    fundamental: FundamentalSnapshot | None
    technical: TechnicalSnapshot | None
    quant: QuantSnapshot | None
    alpha: AlphaScore

    def to_dict(self, *, include_series: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ticker": self.ticker,
            "company": self.company.to_dict() if self.company else None,
            "as_of": self.as_of.isoformat(),
            "price": self.price_series.last_close,
            "price_source": self.price_series.source,
            "bars_available": len(self.price_series),
            "fundamental": self.fundamental.to_dict() if self.fundamental else None,
            "technical": self.technical.to_dict() if self.technical else None,
            "quant": self.quant.to_dict() if self.quant else None,
            "alpha": self.alpha.to_dict(),
        }
        if include_series:
            payload["series"] = {
                "dates": [d.isoformat() for d in self.price_series.dates],
                "open": self.price_series.opens,
                "high": self.price_series.highs,
                "low": self.price_series.lows,
                "close": self.price_series.closes,
                "volume": self.price_series.volumes,
            }
        return payload


def analyze_stock(
    session: Session,
    ticker: str,
    *,
    as_of: date | None = None,
    quant_snapshot: QuantSnapshot | None = None,
    peer_metrics: dict[str, list[float]] | None = None,
    benchmark_series: PriceSeries | None = None,
) -> StockAnalysis:
    """Run every engine for one stock and combine into the master score."""
    as_of = as_of or date.today()
    ticker = ticker.upper()
    company = session.scalar(select(Company).where(Company.ticker == ticker))
    series = load_price_series(session, ticker, as_of=as_of)
    periods = load_financial_periods(session, ticker, as_of=as_of)

    # --- Technical -----------------------------------------------------------
    technical: TechnicalSnapshot | None = None
    if series.dates:
        bench_closes = None
        if benchmark_series and len(benchmark_series) > 0:
            bench_closes = _align_benchmark(series.dates, benchmark_series)
        technical = analyze_technical(
            ticker, series.dates, series.opens, series.highs, series.lows,
            series.closes, series.volumes, benchmark_closes=bench_closes,
        )

    # --- Catalysts (needed before fundamentals, which weight them) -----------
    events = load_catalyst_events(session, ticker, as_of=as_of)
    from backend.analytics.master_score import score_catalysts
    catalyst_result, _ = score_catalysts(events, as_of=as_of)

    # --- Fundamental ---------------------------------------------------------
    fundamental = analyze_fundamentals(
        ticker, periods,
        price=series.last_close,
        shares_outstanding=company.shares_outstanding if company else None,
        peer_metrics=peer_metrics,
        history=_own_valuation_history(session, ticker, as_of),
        catalyst_score=catalyst_result.value,
    )

    # --- Risk inputs ---------------------------------------------------------
    fundamentals_retrieved = None
    staleness_days = None
    if periods:
        rows = session.execute(
            select(FinancialStatement.retrieved_at).where(
                FinancialStatement.ticker == ticker,
                FinancialStatement.period == periods[0].period,
            )
        ).scalars().all()
        if rows:
            fundamentals_retrieved = max(rows)
        staleness_days = (as_of - periods[0].period_end).days

    risk_inputs = RiskInputs(
        volatility_annual=technical.volatility_20d if technical else None,
        max_drawdown_1y=_max_drawdown(series.closes[-252:]),
        average_turnover=_turnover(series),
        debt_to_equity=_metric_value(fundamental, "debt_to_equity"),
        net_debt_to_ebitda=_metric_value(fundamental, "net_debt_to_ebitda"),
        interest_coverage=_metric_value(fundamental, "interest_coverage"),
        data_staleness_days=staleness_days,
    )

    alpha = compute_alpha_score(
        ticker,
        fundamental=fundamental,
        technical=technical,
        quant=quant_snapshot,
        catalyst_events=events,
        risk_inputs=risk_inputs,
        sentiment=load_sentiment(session, ticker, as_of=as_of),
        as_of=as_of,
        fundamentals_retrieved_at=fundamentals_retrieved,
        prices_retrieved_at=series.retrieved_at,
    )

    return StockAnalysis(
        ticker=ticker, company=company, as_of=as_of, price_series=series,
        fundamental=fundamental, technical=technical, quant=quant_snapshot, alpha=alpha,
    )


def _metric_value(snapshot: FundamentalSnapshot | None, name: str) -> float | None:
    if snapshot is None:
        return None
    metric = snapshot.metrics.get(name)
    return metric.value if metric and metric.available else None


def _align_benchmark(dates: Sequence[date], benchmark: PriceSeries) -> list[float | None]:
    """Align benchmark closes to the stock's trading dates (forward-fill only).

    Forward-fill is safe here — it uses the last *known* benchmark value, never
    a future one — which keeps relative strength free of look-ahead.
    """
    lookup = dict(zip(benchmark.dates, benchmark.closes))
    out: list[float | None] = []
    last: float | None = None
    for d in dates:
        if d in lookup and lookup[d] is not None:
            last = lookup[d]
        out.append(last)
    return out


def _own_valuation_history(
    session: Session, ticker: str, as_of: date
) -> dict[str, list[float]]:
    """The company's own past valuation multiples, for historical comparison."""
    from backend.data.models import ValuationSnapshot

    rows = session.execute(
        select(ValuationSnapshot)
        .where(ValuationSnapshot.ticker == ticker.upper(), ValuationSnapshot.date <= as_of)
        .order_by(ValuationSnapshot.date.desc())
        .limit(500)
    ).scalars().all()
    history: dict[str, list[float]] = {}
    for key in ("pe", "pb", "ev_ebitda"):
        values = [getattr(r, key) for r in rows if getattr(r, key) is not None]
        if values:
            history[key] = values
    return history


def build_peer_metrics(
    universe_metrics: dict[str, dict[str, float | None]],
    sector_map: dict[str, str],
    ticker: str,
    *,
    keys: Sequence[str] = (
        "pe", "pb", "ps", "ev_ebitda", "roe", "net_margin", "dividend_yield",
    ),
    min_peers: int = 3,
) -> dict[str, list[float]]:
    """Peer values from the *same sector* only.

    Comparing a bank's P/B to a developer's would be meaningless, so a sector
    with too few members yields no peer context rather than a bad one.
    """
    sector = sector_map.get(ticker.upper())
    if not sector:
        return {}
    peers = [
        t for t, s in sector_map.items()
        if s == sector and t != ticker.upper() and t in universe_metrics
    ]
    if len(peers) < min_peers:
        return {}
    out: dict[str, list[float]] = {}
    for key in keys:
        values = [
            universe_metrics[t][key] for t in peers
            if is_available(universe_metrics[t].get(key))
        ]
        if len(values) >= min_peers:
            out[key] = values
    return out


def analyze_all(
    session: Session,
    *,
    index: str = "all",
    as_of: date | None = None,
    persist: bool = True,
) -> dict[str, StockAnalysis]:
    """Run the full analytical stack across the universe.

    Order matters: the factor model needs the whole cross-section before any
    single name can be scored relative to it.
    """
    as_of = as_of or date.today()
    settings = get_settings()
    companies = get_universe(session, index)
    tickers = [c.ticker for c in companies]
    sector_map = {c.ticker: c.sector or "Unknown" for c in companies}

    universe_metrics = build_universe_metrics(session, tickers, as_of=as_of)
    quant_snapshots = analyze_universe(universe_metrics)
    benchmark = load_price_series(session, settings.benchmark_ticker, as_of=as_of)

    results: dict[str, StockAnalysis] = {}
    for ticker in tickers:
        analysis = analyze_stock(
            session, ticker, as_of=as_of,
            quant_snapshot=quant_snapshots.get(ticker),
            peer_metrics=build_peer_metrics(universe_metrics, sector_map, ticker),
            benchmark_series=benchmark if len(benchmark) else None,
        )
        results[ticker] = analysis
        if persist:
            persist_score(session, analysis)

    if persist:
        session.flush()
    logger.info("Analysed %d names as of %s", len(results), as_of)
    return results


def persist_score(session: Session, analysis: StockAnalysis) -> ScoreHistory:
    """Append the score to history so week-over-week deltas can be computed."""
    alpha = analysis.alpha
    existing = session.scalar(
        select(ScoreHistory).where(
            ScoreHistory.ticker == analysis.ticker, ScoreHistory.as_of == analysis.as_of
        )
    )
    payload = {
        "alpha_score": alpha.value,
        "fundamental_score": alpha.fundamental.value if alpha.fundamental else None,
        "technical_score": alpha.technical.value if alpha.technical else None,
        "quant_score": alpha.quant.value if alpha.quant else None,
        "catalyst_score": alpha.catalyst.value if alpha.catalyst else None,
        "quality_score": alpha.quality_value,
        "risk_score": alpha.risk.value if alpha.risk else None,
        "sentiment_score": alpha.sentiment_value,
        "confidence": alpha.score.confidence.value,
        "breakdown": alpha.score.to_dict(),
    }
    if existing:
        for key, value in payload.items():
            setattr(existing, key, value)
        return existing
    record = ScoreHistory(ticker=analysis.ticker, as_of=analysis.as_of, **payload)
    session.add(record)
    return record


def score_change(
    session: Session, ticker: str, *, as_of: date, lookback_days: int = 7
) -> dict[str, Any] | None:
    """Week-over-week score delta, used by the weekly report and alerts."""
    current = session.scalar(
        select(ScoreHistory)
        .where(ScoreHistory.ticker == ticker.upper(), ScoreHistory.as_of <= as_of)
        .order_by(ScoreHistory.as_of.desc())
    )
    if current is None:
        return None
    previous = session.scalar(
        select(ScoreHistory)
        .where(
            ScoreHistory.ticker == ticker.upper(),
            ScoreHistory.as_of < current.as_of,
            ScoreHistory.as_of >= current.as_of - timedelta(days=lookback_days * 3),
        )
        .order_by(ScoreHistory.as_of.desc())
    )
    if previous is None or current.alpha_score is None or previous.alpha_score is None:
        return None
    return {
        "ticker": ticker.upper(),
        "from": round(previous.alpha_score, 1),
        "to": round(current.alpha_score, 1),
        "delta": round(current.alpha_score - previous.alpha_score, 1),
        "from_date": previous.as_of.isoformat(),
        "to_date": current.as_of.isoformat(),
        "components": {
            "fundamental": _delta(previous.fundamental_score, current.fundamental_score),
            "technical": _delta(previous.technical_score, current.technical_score),
            "quant": _delta(previous.quant_score, current.quant_score),
            "catalyst": _delta(previous.catalyst_score, current.catalyst_score),
            "risk": _delta(previous.risk_score, current.risk_score),
        },
    }


def _delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    return round(after - before, 1)
