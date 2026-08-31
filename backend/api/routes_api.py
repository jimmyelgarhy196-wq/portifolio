"""JSON API routes.

Everything the UI renders is also available as JSON, so the terminal can be
scripted and every score can be audited programmatically.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from backend.analytics.quant import analyze_universe
from backend.analytics.service import (
    analyze_stock,
    build_peer_metrics,
    build_universe_metrics,
    load_price_series,
    persist_score,
    score_change,
)
from backend.api.schemas import uses_synthetic_data
from backend.core.config import get_settings
from backend.core.database import get_db
from backend.data.models import (
    Alert,
    BacktestRun,
    Company,
    DataQualityLog,
    Disclosure,
    NewsItem,
    Portfolio,
    Position,
    Recommendation,
    Report,
    ResearchThesis,
    ScoreHistory,
    Trade,
    WatchlistItem,
)
from backend.data.providers.registry import provider_status
from backend.data.universe import get_universe, sectors, universe_status

router = APIRouter(prefix="/api", tags=["api"])


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    from backend.billing.payments import payment_status
    from backend.market.quotes import build_quote_provider
    from backend.notify.email_service import email_status

    provider = build_quote_provider(db)
    return {
        "status": "ok",
        # What this deployment is permitted to do, stated positively.
        "mode": "RESEARCH_AND_INFORMATION_ONLY",
        "live_trading": False,
        "holds_client_funds": False,
        "holds_securities": False,
        "executes_trades": False,
        "ai_enabled": settings.ai_enabled,
        "synthetic_data": uses_synthetic_data(db),
        "quote_provider": {
            "name": provider.name,
            "is_demo": provider.is_demo,
            "delayed_minutes": provider.delayed_minutes,
        },
        "email_provider": email_status()["provider"],
        "payments_enabled": payment_status()["processes_payments"],
        "universe": universe_status(db).to_dict(),
        "companies": db.scalar(select(func.count()).select_from(Company)) or 0,
    }


@router.get("/providers")
def providers() -> list[dict[str, Any]]:
    return provider_status()


@router.get("/data-quality")
def data_quality(
    limit: int = Query(60, le=500), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(DataQualityLog).order_by(desc(DataQualityLog.created_at)).limit(limit)
    ).scalars().all()
    return [r.to_dict() for r in rows]


# ---------------------------------------------------------------------------
# Universe and stocks
# ---------------------------------------------------------------------------
@router.get("/universe")
def universe(
    index: str = Query("all"), db: Session = Depends(get_db)
) -> dict[str, Any]:
    companies = get_universe(db, index)
    return {
        "index": index,
        "status": universe_status(db).to_dict(),
        "sectors": sectors(db),
        "companies": [c.to_dict() for c in companies],
    }


@router.get("/stocks/{ticker}")
def stock_detail(
    ticker: str,
    include_series: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    company = db.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        raise HTTPException(404, f"{ticker.upper()} is not in the universe.")

    settings = get_settings()
    peers = get_universe(db, "all")
    sector_map = {c.ticker: c.sector or "Unknown" for c in peers}
    universe_metrics = build_universe_metrics(db, [c.ticker for c in peers])
    quant = analyze_universe(universe_metrics)
    benchmark = load_price_series(db, settings.benchmark_ticker)

    analysis = analyze_stock(
        db, ticker,
        quant_snapshot=quant.get(ticker.upper()),
        peer_metrics=build_peer_metrics(universe_metrics, sector_map, ticker),
        benchmark_series=benchmark if len(benchmark) else None,
    )
    payload = analysis.to_dict(include_series=include_series)
    payload["score_change"] = score_change(db, ticker, as_of=analysis.as_of)
    return payload


@router.get("/stocks/{ticker}/prices")
def stock_prices(
    ticker: str,
    days: int = Query(400, le=4000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    series = load_price_series(db, ticker, lookback_days=days)
    return {
        "ticker": ticker.upper(),
        "source": series.source,
        "dates": [d.isoformat() for d in series.dates],
        "open": series.opens, "high": series.highs, "low": series.lows,
        "close": series.closes, "volume": series.volumes,
    }


@router.get("/stocks/{ticker}/news")
def stock_news(
    ticker: str, limit: int = Query(30, le=200), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(NewsItem)
        .where(NewsItem.ticker == ticker.upper())
        .order_by(desc(NewsItem.publication_date))
        .limit(limit)
    ).scalars().all()
    return [r.to_dict() for r in rows]


@router.get("/stocks/{ticker}/disclosures")
def stock_disclosures(
    ticker: str, limit: int = Query(30, le=200), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Disclosure)
        .where(Disclosure.ticker == ticker.upper())
        .order_by(desc(Disclosure.date))
        .limit(limit)
    ).scalars().all()
    return [r.to_dict() for r in rows]


@router.get("/scores")
def scores(
    index: str = Query("all"),
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Latest stored score per ticker."""
    tickers = [c.ticker for c in get_universe(db, index)]
    latest = (
        select(ScoreHistory.ticker, func.max(ScoreHistory.as_of).label("as_of"))
        .where(ScoreHistory.ticker.in_(tickers))
        .group_by(ScoreHistory.ticker)
        .subquery()
    )
    rows = db.execute(
        select(ScoreHistory)
        .join(
            latest,
            (ScoreHistory.ticker == latest.c.ticker) & (ScoreHistory.as_of == latest.c.as_of),
        )
        .order_by(desc(ScoreHistory.alpha_score))
        .limit(limit)
    ).scalars().all()
    return [
        {
            "ticker": r.ticker, "as_of": r.as_of.isoformat(),
            "alpha_score": r.alpha_score, "fundamental_score": r.fundamental_score,
            "technical_score": r.technical_score, "quant_score": r.quant_score,
            "catalyst_score": r.catalyst_score, "risk_score": r.risk_score,
            "confidence": r.confidence,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
@router.get("/portfolio")
def portfolio(db: Session = Depends(get_db)) -> dict[str, Any]:
    from backend.portfolio.attribution import analyze_attribution
    from backend.portfolio.paper_trading import get_or_create_portfolio, mark_to_market

    p = get_or_create_portfolio(db)
    state = mark_to_market(db, p)
    attribution = analyze_attribution(db, p)
    return {
        "name": p.name, "mode": p.mode, "currency": p.currency,
        "initial_capital": p.initial_capital,
        "cash": state["cash"], "total_value": state["total_value"],
        "cash_weight": state["cash_weight"],
        "gross_exposure": state["gross_exposure"],
        "net_exposure": state["net_exposure"],
        "unrealized_pnl": state["unrealized_pnl"],
        "realized_pnl": state["realized_pnl"],
        "total_return": state["total_return"],
        "unpriced_tickers": state["unpriced_tickers"],
        "positions": [pos.to_dict() for pos in state["positions"]],
        "attribution": attribution.to_dict(),
    }


@router.get("/portfolio/risk")
def portfolio_risk(db: Session = Depends(get_db)) -> dict[str, Any]:
    from backend.portfolio.paper_trading import get_or_create_portfolio
    from backend.portfolio.risk import analyze_risk

    p = get_or_create_portfolio(db)
    return analyze_risk(db, p).to_dict()


@router.get("/portfolio/trades")
def portfolio_trades(
    limit: int = Query(100, le=1000), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Trade).order_by(desc(Trade.executed_at)).limit(limit)
    ).scalars().all()
    return [r.to_dict() for r in rows]


@router.post("/portfolio/trade")
def place_paper_trade(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Execute a PAPER trade. There is no live execution path in this system."""
    from backend.portfolio.paper_trading import (
        TradeRejected,
        execute_trade,
        get_or_create_portfolio,
    )

    ticker = str(payload.get("ticker", "")).strip().upper()
    side = str(payload.get("side", "")).strip().upper()
    if not ticker or not side:
        raise HTTPException(400, "Both 'ticker' and 'side' are required.")
    try:
        quantity = float(payload.get("quantity", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "'quantity' must be a number.")

    p = get_or_create_portfolio(db)
    try:
        trade = execute_trade(
            db, p, ticker=ticker, side=side, quantity=quantity,
            price=payload.get("price"), strategy=payload.get("strategy"),
            thesis_id=payload.get("thesis_id"), note=payload.get("note"),
        )
    except TradeRejected as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return {"ok": True, "mode": "PAPER", "trade": trade.to_dict()}


@router.post("/portfolio/size")
def size_position(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Compute a recommended position size with its full reasoning."""
    from backend.portfolio.paper_trading import get_or_create_portfolio, mark_to_market
    from backend.portfolio.sizing import SizingInputs, size_position as compute

    ticker = str(payload.get("ticker", "")).strip().upper()
    if not ticker:
        raise HTTPException(400, "'ticker' is required.")

    p = get_or_create_portfolio(db)
    state = mark_to_market(db, p)
    company = db.scalar(select(Company).where(Company.ticker == ticker))
    sector = company.sector if company else None

    sector_weight = sum(
        abs(pos.market_value or 0) / state["total_value"]
        for pos in state["positions"]
        if pos.sector == sector and state["total_value"]
    )
    speculative = {"technical_swing", "special_situations", "bearish_short"}
    speculative_weight = sum(
        abs(pos.market_value or 0) / state["total_value"]
        for pos in state["positions"]
        if (pos.strategy in speculative) and state["total_value"]
    )

    result = compute(SizingInputs(
        ticker=ticker,
        direction=str(payload.get("direction", "LONG")),
        strategy=str(payload.get("strategy", "fundamental_long")),
        sector=sector,
        conviction=_float(payload.get("conviction")),
        entry_price=_float(payload.get("entry_price")),
        target_price=_float(payload.get("target_price")),
        invalidation_price=_float(payload.get("invalidation_price")),
        annual_volatility=_float(payload.get("annual_volatility")),
        average_turnover=_float(payload.get("average_turnover")),
        correlated_holdings=int(payload.get("correlated_holdings", 0) or 0),
        current_sector_weight=sector_weight,
        current_cash_weight=state["cash_weight"] or 0.0,
        current_speculative_weight=speculative_weight,
        portfolio_value=state["total_value"],
    ))
    return result.to_dict()


def _float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------
@router.get("/theses")
def theses(
    status: str | None = Query(None), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    stmt = select(ResearchThesis)
    if status:
        stmt = stmt.where(ResearchThesis.status == status.upper())
    rows = db.execute(
        stmt.order_by(desc(ResearchThesis.alpha_score))
    ).scalars().all()
    return [r.to_dict() for r in rows]


@router.get("/theses/{thesis_id}")
def thesis_detail(thesis_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    thesis = db.get(ResearchThesis, thesis_id)
    if thesis is None:
        raise HTTPException(404, "Thesis not found.")
    from backend.research.thesis import render_thesis

    return {
        **thesis.to_dict(),
        "rendered": render_thesis(thesis),
        "versions": [
            {
                "version": v.version,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "change_summary": v.change_summary,
            }
            for v in thesis.versions
        ],
    }


@router.post("/research/{ticker}")
def run_research_for(ticker: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Run the agent pipeline on one name."""
    from backend.research.pipeline import run_research

    company = db.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        raise HTTPException(404, f"{ticker.upper()} is not in the universe.")
    run = run_research(db, tickers=[ticker.upper()], index="all")
    db.commit()
    if not run.results:
        raise HTTPException(
            422,
            "Research produced no result. "
            + (run.errors[0] if run.errors else "Insufficient data for this name."),
        )
    return run.results[0].to_dict()


@router.get("/recommendations")
def recommendations(
    limit: int = Query(100, le=1000),
    strategy: str | None = Query(None),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(Recommendation)
    if strategy:
        stmt = stmt.where(Recommendation.strategy == strategy)
    rows = db.execute(
        stmt.order_by(desc(Recommendation.created_at)).limit(limit)
    ).scalars().all()
    return [r.to_dict() for r in rows]


# ---------------------------------------------------------------------------
# Watchlist and alerts
# ---------------------------------------------------------------------------
@router.get("/watchlist")
def watchlist(db: Session = Depends(get_db)) -> dict[str, list[dict[str, Any]]]:
    rows = db.execute(select(WatchlistItem).order_by(WatchlistItem.list_name)).scalars().all()
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row.list_name, []).append(row.to_dict())
    return out


@router.post("/watchlist")
def add_watchlist(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
) -> dict[str, Any]:
    ticker = str(payload.get("ticker", "")).strip().upper()
    list_name = str(payload.get("list_name", "core")).strip().lower()
    if not ticker:
        raise HTTPException(400, "'ticker' is required.")
    existing = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.ticker == ticker, WatchlistItem.list_name == list_name
        )
    )
    if existing:
        existing.note = payload.get("note", existing.note)
        existing.target_price = _float(payload.get("target_price")) or existing.target_price
        item = existing
    else:
        item = WatchlistItem(
            ticker=ticker, list_name=list_name, note=payload.get("note"),
            target_price=_float(payload.get("target_price")),
        )
        db.add(item)
    db.commit()
    return item.to_dict()


@router.delete("/watchlist/{item_id}")
def remove_watchlist(item_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    item = db.get(WatchlistItem, item_id)
    if item is None:
        raise HTTPException(404, "Watchlist item not found.")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.get("/alerts")
def alerts(
    status: str | None = Query(None),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(Alert)
    if status:
        stmt = stmt.where(Alert.status == status.upper())
    rows = db.execute(stmt.order_by(desc(Alert.created_at)).limit(limit)).scalars().all()
    return [r.to_dict() for r in rows]


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(404, "Alert not found.")
    alert.status = "ACK"
    db.commit()
    return alert.to_dict()


# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------
@router.get("/backtests")
def backtests(
    limit: int = Query(50, le=200), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(BacktestRun).order_by(desc(BacktestRun.created_at)).limit(limit)
    ).scalars().all()
    return [r.to_dict() for r in rows]


@router.get("/backtests/{run_id}")
def backtest_detail(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(404, "Backtest run not found.")
    return {**run.to_dict(), "equity_curve": run.equity_curve, "trades": run.trades}


@router.post("/backtests")
def create_backtest(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
) -> dict[str, Any]:
    from backend.backtesting.engine import BacktestConfig, persist_backtest, run_backtest

    strategy = str(payload.get("strategy", "fundamental_long"))
    config = BacktestConfig(
        strategy=strategy,
        start=_date(payload.get("start")),
        end=_date(payload.get("end")),
        initial_capital=float(payload.get("initial_capital") or 1_000_000),
        rebalance=str(payload.get("rebalance", "monthly")),
        index=str(payload.get("index", "egx30")),
        params=payload.get("params") or {},
    )
    try:
        result = run_backtest(db, config)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    run = persist_backtest(db, result, name=payload.get("name"))
    db.commit()
    return {"run_id": run.id, **result.to_dict(include_curve=False)}


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise HTTPException(400, f"Invalid date: {value!r}. Use YYYY-MM-DD.")


# ---------------------------------------------------------------------------
# Reports and evaluation
# ---------------------------------------------------------------------------
@router.get("/reports")
def reports(
    limit: int = Query(50, le=200), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Report).order_by(desc(Report.created_at)).limit(limit)
    ).scalars().all()
    return [r.to_dict() for r in rows]


@router.get("/reports/{report_id}")
def report_detail(report_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "Report not found.")
    return {**report.to_dict(), "markdown": report.markdown, "sections": report.sections}


@router.get("/evaluation")
def evaluation(db: Session = Depends(get_db)) -> dict[str, Any]:
    from backend.reports.evaluation import evaluate_model

    return evaluate_model(db).to_dict()
