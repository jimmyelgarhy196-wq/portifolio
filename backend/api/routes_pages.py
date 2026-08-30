"""HTML page routes for the terminal."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from backend.analytics.quant import analyze_universe
from backend.analytics.service import (
    analyze_stock,
    build_peer_metrics,
    build_universe_metrics,
    load_price_series,
    score_change,
)
from backend.api.deps import base_context, render
from backend.core.config import get_settings, load_yaml_config
from backend.core.database import get_db
from backend.data.models import (
    Alert,
    BacktestRun,
    Company,
    DataQualityLog,
    Disclosure,
    NewsItem,
    Portfolio,
    Recommendation,
    Report,
    ResearchThesis,
    ScoreHistory,
    Trade,
    WatchlistItem,
)
from backend.data.providers.registry import provider_status
from backend.data.universe import get_universe, sectors

router = APIRouter()

WATCHLISTS = ("core", "swing", "short", "special_situations")
WATCHLIST_LABELS = {
    "core": "Core", "swing": "Swing",
    "short": "Short (paper)", "special_situations": "Special Situations",
}


def _latest_scores(db: Session, tickers: list[str] | None = None) -> dict[str, ScoreHistory]:
    latest = (
        select(ScoreHistory.ticker, func.max(ScoreHistory.as_of).label("as_of"))
        .group_by(ScoreHistory.ticker)
        .subquery()
    )
    stmt = select(ScoreHistory).join(
        latest,
        (ScoreHistory.ticker == latest.c.ticker) & (ScoreHistory.as_of == latest.c.as_of),
    )
    if tickers:
        stmt = stmt.where(ScoreHistory.ticker.in_(tickers))
    return {r.ticker: r for r in db.execute(stmt).scalars().all()}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> Any:
    from backend.portfolio.attribution import analyze_attribution
    from backend.portfolio.paper_trading import get_or_create_portfolio, mark_to_market
    from backend.portfolio.risk import analyze_risk

    portfolio = get_or_create_portfolio(db)
    state = mark_to_market(db, portfolio)
    attribution = analyze_attribution(db, portfolio)
    risk = analyze_risk(db, portfolio)

    scores = _latest_scores(db)
    companies = {c.ticker: c for c in get_universe(db, "all")}
    held = {p.ticker for p in state["positions"]}

    opportunities = []
    for ticker, row in sorted(
        scores.items(), key=lambda kv: -(kv[1].alpha_score or 0)
    ):
        if row.alpha_score is None or ticker in held:
            continue
        thesis = db.scalar(
            select(ResearchThesis)
            .where(ResearchThesis.ticker == ticker)
            .order_by(desc(ResearchThesis.updated_at))
        )
        price = _price(db, ticker)
        upside = None
        if thesis and thesis.target_price and price:
            upside = (thesis.target_price - price) / price
        opportunities.append({
            "ticker": ticker,
            "name": companies[ticker].name if ticker in companies else ticker,
            "score": row.alpha_score,
            "price": price,
            "target": thesis.target_price if thesis else None,
            "upside": upside,
            "strategy": thesis.strategy if thesis else None,
            "conviction": thesis.conviction if thesis else None,
        })
        if len(opportunities) >= 8:
            break

    positions = []
    for position in state["positions"]:
        thesis = db.scalar(
            select(ResearchThesis).where(
                ResearchThesis.ticker == position.ticker,
                ResearchThesis.direction == position.direction,
            ).order_by(desc(ResearchThesis.updated_at))
        )
        positions.append({
            "position": position,
            "thesis": thesis,
            "score": scores.get(position.ticker).alpha_score if position.ticker in scores else None,
            "return_pct": (
                (position.current_price - position.average_price) / position.average_price
                if position.current_price and position.average_price else None
            ),
        })
    positions.sort(key=lambda p: -(p["position"].portfolio_weight or 0))

    recent_alerts = db.execute(
        select(Alert).where(Alert.status == "NEW")
        .order_by(desc(Alert.created_at)).limit(6)
    ).scalars().all()

    latest_report = db.scalar(select(Report).order_by(desc(Report.created_at)))
    week_ago = date.today() - timedelta(days=8)
    new_theses = db.scalar(
        select(func.count()).select_from(ResearchThesis)
        .where(ResearchThesis.created_at >= week_ago)
    ) or 0
    changed_theses = db.scalar(
        select(func.count()).select_from(ResearchThesis)
        .where(ResearchThesis.version > 1, ResearchThesis.updated_at >= week_ago)
    ) or 0

    context = base_context(
        request, db, active="dashboard",
        portfolio=portfolio, state=state, attribution=attribution, risk=risk,
        opportunities=opportunities, positions=positions, alerts=recent_alerts,
        latest_report=latest_report,
        research_summary={
            "new_theses": new_theses,
            "changed_theses": changed_theses,
            "risk_alerts": sum(1 for a in recent_alerts if a.severity in ("warning", "critical")),
        },
    )
    return render(request, "dashboard.html", context)


def _price(db: Session, ticker: str) -> float | None:
    from backend.portfolio.paper_trading import latest_price

    return latest_price(db, ticker)


# ---------------------------------------------------------------------------
# Opportunities scanner
# ---------------------------------------------------------------------------
@router.get("/opportunities", response_class=HTMLResponse)
def opportunities(
    request: Request,
    min_score: float | None = Query(None),
    max_pe: float | None = Query(None),
    min_roe: float | None = Query(None),
    min_revenue_growth: float | None = Query(None),
    max_debt_ebitda: float | None = Query(None),
    rsi_min: float | None = Query(None),
    rsi_max: float | None = Query(None),
    min_market_cap: float | None = Query(None),
    sector: str | None = Query(None),
    strategy: str | None = Query(None),
    index: str = Query("all"),
    db: Session = Depends(get_db),
) -> Any:
    companies = get_universe(db, index)
    tickers = [c.ticker for c in companies]
    universe_metrics = build_universe_metrics(db, tickers)
    scores = _latest_scores(db, tickers)

    theses = {}
    for thesis in db.execute(select(ResearchThesis)).scalars().all():
        current = theses.get(thesis.ticker)
        if current is None or (thesis.updated_at and current.updated_at and thesis.updated_at > current.updated_at):
            theses[thesis.ticker] = thesis

    rows = []
    for company in companies:
        metrics = universe_metrics.get(company.ticker, {})
        score_row = scores.get(company.ticker)
        thesis = theses.get(company.ticker)
        alpha = score_row.alpha_score if score_row else None

        # Filters. A name lacking the metric a filter targets is excluded, since
        # including it would silently treat "unknown" as "passes".
        if min_score is not None and (alpha is None or alpha < min_score):
            continue
        if max_pe is not None and (metrics.get("pe") is None or metrics["pe"] > max_pe):
            continue
        if min_roe is not None and (metrics.get("roe") is None or metrics["roe"] < min_roe):
            continue
        if min_revenue_growth is not None and (
            metrics.get("revenue_growth") is None or metrics["revenue_growth"] < min_revenue_growth
        ):
            continue
        if max_debt_ebitda is not None and (
            metrics.get("debt_to_equity") is None or metrics["debt_to_equity"] > max_debt_ebitda
        ):
            continue
        if min_market_cap is not None and (
            metrics.get("market_cap") is None or metrics["market_cap"] < min_market_cap
        ):
            continue
        if sector and company.sector != sector:
            continue
        if strategy and (thesis is None or thesis.strategy != strategy):
            continue

        price = _price(db, company.ticker)
        rsi = None
        if rsi_min is not None or rsi_max is not None:
            series = load_price_series(db, company.ticker, lookback_days=200)
            from backend.analytics.indicators import last_valid, rsi as rsi_fn

            rsi = last_valid(rsi_fn(series.closes, 14))
            if rsi is None:
                continue
            if rsi_min is not None and rsi < rsi_min:
                continue
            if rsi_max is not None and rsi > rsi_max:
                continue

        rows.append({
            "company": company,
            "score": alpha,
            "fundamental": score_row.fundamental_score if score_row else None,
            "technical": score_row.technical_score if score_row else None,
            "quant": score_row.quant_score if score_row else None,
            "confidence": score_row.confidence if score_row else None,
            "price": price,
            "pe": metrics.get("pe"),
            "roe": metrics.get("roe"),
            "revenue_growth": metrics.get("revenue_growth"),
            "market_cap": metrics.get("market_cap"),
            "rsi": rsi,
            "thesis": thesis,
            "upside": (
                (thesis.target_price - price) / price
                if thesis and thesis.target_price and price else None
            ),
        })

    rows.sort(key=lambda r: -(r["score"] or 0))
    context = base_context(
        request, db, active="opportunities",
        rows=rows, sectors=sectors(db),
        strategies=["fundamental_long", "technical_swing", "special_situations", "bearish_short"],
        filters={
            "min_score": min_score, "max_pe": max_pe, "min_roe": min_roe,
            "min_revenue_growth": min_revenue_growth, "max_debt_ebitda": max_debt_ebitda,
            "rsi_min": rsi_min, "rsi_max": rsi_max, "min_market_cap": min_market_cap,
            "sector": sector, "strategy": strategy, "index": index,
        },
        total=len(companies),
    )
    return render(request, "opportunities.html", context)


# ---------------------------------------------------------------------------
# Stock detail
# ---------------------------------------------------------------------------
@router.get("/stocks", response_class=HTMLResponse)
def stock_list(
    request: Request, index: str = Query("all"), db: Session = Depends(get_db)
) -> Any:
    companies = get_universe(db, index)
    scores = _latest_scores(db, [c.ticker for c in companies])
    rows = [
        {
            "company": c,
            "score": scores.get(c.ticker).alpha_score if c.ticker in scores else None,
            "price": _price(db, c.ticker),
        }
        for c in companies
    ]
    rows.sort(key=lambda r: -(r["score"] or 0))
    context = base_context(request, db, active="stocks", rows=rows, index=index)
    return render(request, "stocks.html", context)


@router.get("/stocks/{ticker}", response_class=HTMLResponse)
def stock_detail(request: Request, ticker: str, db: Session = Depends(get_db)) -> Any:
    ticker = ticker.upper()
    company = db.scalar(select(Company).where(Company.ticker == ticker))
    if company is None:
        raise HTTPException(404, f"{ticker} is not in the universe.")

    settings = get_settings()
    peers = get_universe(db, "all")
    sector_map = {c.ticker: c.sector or "Unknown" for c in peers}
    universe_metrics = build_universe_metrics(db, [c.ticker for c in peers])
    quant = analyze_universe(universe_metrics)
    benchmark = load_price_series(db, settings.benchmark_ticker)

    analysis = analyze_stock(
        db, ticker,
        quant_snapshot=quant.get(ticker),
        peer_metrics=build_peer_metrics(universe_metrics, sector_map, ticker),
        benchmark_series=benchmark if len(benchmark) else None,
    )

    thesis = db.scalar(
        select(ResearchThesis).where(ResearchThesis.ticker == ticker)
        .order_by(desc(ResearchThesis.updated_at))
    )
    news = db.execute(
        select(NewsItem).where(NewsItem.ticker == ticker)
        .order_by(desc(NewsItem.publication_date)).limit(12)
    ).scalars().all()
    disclosures = db.execute(
        select(Disclosure).where(Disclosure.ticker == ticker)
        .order_by(desc(Disclosure.date)).limit(12)
    ).scalars().all()

    from backend.analytics.indicators import ema, macd, rsi, sma

    closes = analysis.price_series.closes
    series_payload = {
        "dates": [d.isoformat() for d in analysis.price_series.dates],
        "close": closes,
        "volume": analysis.price_series.volumes,
        "sma20": sma(closes, 20), "sma50": sma(closes, 50), "sma200": sma(closes, 200),
        "ema20": ema(closes, 20),
        "rsi": rsi(closes, 14),
    }
    macd_line, signal_line, histogram = macd(closes)
    series_payload.update({"macd": macd_line, "macd_signal": signal_line, "macd_hist": histogram})

    context = base_context(
        request, db, active="stocks",
        company=company, analysis=analysis, thesis=thesis,
        news=news, disclosures=disclosures,
        series=series_payload,
        score_delta=score_change(db, ticker, as_of=analysis.as_of),
        in_watchlists=[
            w.list_name for w in db.execute(
                select(WatchlistItem).where(WatchlistItem.ticker == ticker)
            ).scalars().all()
        ],
        watchlists=WATCHLISTS, watchlist_labels=WATCHLIST_LABELS,
    )
    return render(request, "stock_detail.html", context)


# ---------------------------------------------------------------------------
# Portfolio, risk, paper trading
# ---------------------------------------------------------------------------
@router.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request, db: Session = Depends(get_db)) -> Any:
    from backend.portfolio.attribution import analyze_attribution
    from backend.portfolio.paper_trading import get_or_create_portfolio, mark_to_market

    portfolio = get_or_create_portfolio(db)
    state = mark_to_market(db, portfolio)
    attribution = analyze_attribution(db, portfolio)
    scores = _latest_scores(db)

    rows = []
    for position in state["positions"]:
        thesis = db.scalar(
            select(ResearchThesis).where(
                ResearchThesis.ticker == position.ticker,
                ResearchThesis.direction == position.direction,
            ).order_by(desc(ResearchThesis.updated_at))
        )
        rows.append({
            "position": position, "thesis": thesis,
            "score": scores.get(position.ticker).alpha_score if position.ticker in scores else None,
            "return_pct": (
                (position.current_price - position.average_price) / position.average_price
                if position.current_price and position.average_price else None
            ),
        })
    rows.sort(key=lambda r: -(r["position"].portfolio_weight or 0))

    trades = db.execute(
        select(Trade).order_by(desc(Trade.executed_at)).limit(40)
    ).scalars().all()

    context = base_context(
        request, db, active="portfolio",
        portfolio=portfolio, state=state, rows=rows,
        attribution=attribution, trades=trades,
    )
    return render(request, "portfolio.html", context)


@router.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request, db: Session = Depends(get_db)) -> Any:
    from backend.portfolio.paper_trading import get_or_create_portfolio
    from backend.portfolio.risk import analyze_risk

    portfolio = get_or_create_portfolio(db)
    report = analyze_risk(db, portfolio)
    limits = load_yaml_config("risk").get("limits", {})
    context = base_context(
        request, db, active="risk", portfolio=portfolio, report=report, limits=limits
    )
    return render(request, "risk.html", context)


@router.get("/paper-trading", response_class=HTMLResponse)
def paper_trading_page(
    request: Request,
    message: str | None = Query(None),
    error: str | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    from backend.portfolio.paper_trading import get_or_create_portfolio, mark_to_market

    portfolio = get_or_create_portfolio(db)
    state = mark_to_market(db, portfolio)
    trades = db.execute(
        select(Trade).order_by(desc(Trade.executed_at)).limit(60)
    ).scalars().all()
    companies = get_universe(db, "all")
    context = base_context(
        request, db, active="paper_trading",
        portfolio=portfolio, state=state, trades=trades, companies=companies,
        message=message, error=error,
    )
    return render(request, "paper_trading.html", context)


@router.post("/paper-trading/execute")
def execute_paper_trade(
    ticker: str = Form(...),
    side: str = Form(...),
    quantity: float = Form(...),
    strategy: str = Form("fundamental_long"),
    note: str = Form(""),
    db: Session = Depends(get_db),
) -> Any:
    from backend.portfolio.paper_trading import (
        TradeRejected,
        execute_trade,
        get_or_create_portfolio,
    )

    portfolio = get_or_create_portfolio(db)
    try:
        trade = execute_trade(
            db, portfolio, ticker=ticker, side=side, quantity=quantity,
            strategy=strategy, note=note or None,
        )
        db.commit()
        message = (
            f"PAPER {trade.side} {trade.quantity:g} {trade.ticker} at "
            f"{trade.price:,.3f} (commission {trade.commission:,.2f})"
        )
        return RedirectResponse(f"/paper-trading?message={message}", status_code=303)
    except TradeRejected as exc:
        return RedirectResponse(f"/paper-trading?error={exc}", status_code=303)


# ---------------------------------------------------------------------------
# Research, theses
# ---------------------------------------------------------------------------
@router.get("/research", response_class=HTMLResponse)
def research_page(
    request: Request, ticker: str | None = Query(None), db: Session = Depends(get_db)
) -> Any:
    result = None
    if ticker:
        from backend.research.pipeline import run_research

        run = run_research(db, tickers=[ticker.upper()], index="all")
        db.commit()
        result = run.results[0] if run.results else None

    companies = get_universe(db, "all")
    context = base_context(
        request, db, active="research",
        companies=companies, ticker=(ticker or "").upper(), result=result,
    )
    return render(request, "research.html", context)


@router.get("/theses", response_class=HTMLResponse)
def theses_page(
    request: Request, status: str | None = Query(None), db: Session = Depends(get_db)
) -> Any:
    stmt = select(ResearchThesis)
    if status:
        stmt = stmt.where(ResearchThesis.status == status.upper())
    rows = db.execute(
        stmt.order_by(desc(ResearchThesis.alpha_score))
    ).scalars().all()
    context = base_context(request, db, active="theses", theses=rows, status=status)
    return render(request, "theses.html", context)


@router.get("/theses/{thesis_id}", response_class=HTMLResponse)
def thesis_detail_page(
    request: Request, thesis_id: int, db: Session = Depends(get_db)
) -> Any:
    thesis = db.get(ResearchThesis, thesis_id)
    if thesis is None:
        raise HTTPException(404, "Thesis not found.")
    from backend.research.thesis import render_thesis

    context = base_context(
        request, db, active="theses",
        thesis=thesis, rendered=render_thesis(thesis), versions=thesis.versions,
    )
    return render(request, "thesis_detail.html", context)


# ---------------------------------------------------------------------------
# Watchlist, alerts, markets
# ---------------------------------------------------------------------------
@router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, db: Session = Depends(get_db)) -> Any:
    items = db.execute(select(WatchlistItem)).scalars().all()
    scores = _latest_scores(db)
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in WATCHLISTS}
    for item in items:
        grouped.setdefault(item.list_name, []).append({
            "item": item,
            "price": _price(db, item.ticker),
            "score": scores.get(item.ticker).alpha_score if item.ticker in scores else None,
        })
    companies = get_universe(db, "all")
    context = base_context(
        request, db, active="watchlist",
        grouped=grouped, companies=companies,
        watchlists=WATCHLISTS, watchlist_labels=WATCHLIST_LABELS,
    )
    return render(request, "watchlist.html", context)


@router.post("/watchlist/add")
def watchlist_add(
    ticker: str = Form(...),
    list_name: str = Form("core"),
    note: str = Form(""),
    target_price: str = Form(""),
    db: Session = Depends(get_db),
) -> Any:
    ticker = ticker.strip().upper()
    existing = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.ticker == ticker, WatchlistItem.list_name == list_name
        )
    )
    price = None
    try:
        price = float(target_price) if target_price.strip() else None
    except ValueError:
        price = None
    if existing:
        existing.note = note or existing.note
        existing.target_price = price or existing.target_price
    else:
        db.add(WatchlistItem(
            ticker=ticker, list_name=list_name, note=note or None, target_price=price
        ))
    db.commit()
    return RedirectResponse("/watchlist", status_code=303)


@router.post("/watchlist/remove/{item_id}")
def watchlist_remove(item_id: int, db: Session = Depends(get_db)) -> Any:
    item = db.get(WatchlistItem, item_id)
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse("/watchlist", status_code=303)


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(
    request: Request, status: str = Query("NEW"), db: Session = Depends(get_db)
) -> Any:
    stmt = select(Alert)
    if status and status != "ALL":
        stmt = stmt.where(Alert.status == status.upper())
    rows = db.execute(stmt.order_by(desc(Alert.created_at)).limit(200)).scalars().all()
    context = base_context(request, db, active="alerts", alerts=rows, status=status)
    return render(request, "alerts.html", context)


@router.post("/alerts/{alert_id}/ack")
def alert_ack(alert_id: int, db: Session = Depends(get_db)) -> Any:
    alert = db.get(Alert, alert_id)
    if alert:
        alert.status = "ACK"
        db.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.get("/markets", response_class=HTMLResponse)
def markets_page(request: Request, db: Session = Depends(get_db)) -> Any:
    settings = get_settings()
    benchmark = load_price_series(db, settings.benchmark_ticker, lookback_days=400)
    companies = get_universe(db, "all")

    moves = []
    for company in companies:
        series = load_price_series(db, company.ticker, lookback_days=40)
        closes = [c for c in series.closes if c is not None]
        if len(closes) < 2:
            continue
        day = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] else None
        week = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 and closes[-6] else None
        month = (closes[-1] - closes[0]) / closes[0] if closes[0] else None
        moves.append({
            "company": company, "price": closes[-1],
            "day": day, "week": week, "month": month,
            "volume": series.volumes[-1] if series.volumes else None,
        })

    sector_perf: dict[str, list[float]] = {}
    for move in moves:
        if move["week"] is not None:
            sector_perf.setdefault(move["company"].sector or "Unclassified", []).append(move["week"])
    sector_rows = sorted(
        (
            {"sector": s, "average": sum(v) / len(v), "count": len(v)}
            for s, v in sector_perf.items()
        ),
        key=lambda r: -r["average"],
    )

    gainers = sorted([m for m in moves if m["day"] is not None], key=lambda m: -m["day"])[:10]
    losers = sorted([m for m in moves if m["day"] is not None], key=lambda m: m["day"])[:10]
    active = sorted([m for m in moves if m["volume"]], key=lambda m: -m["volume"])[:10]

    context = base_context(
        request, db, active="markets",
        benchmark_ticker=settings.benchmark_ticker,
        benchmark_series={
            "dates": [d.isoformat() for d in benchmark.dates],
            "close": benchmark.closes,
        },
        gainers=gainers, losers=losers, most_active=active, sector_rows=sector_rows,
        total=len(moves),
    )
    return render(request, "markets.html", context)


# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------
@router.get("/backtesting", response_class=HTMLResponse)
def backtesting_page(
    request: Request, run_id: int | None = Query(None), db: Session = Depends(get_db)
) -> Any:
    from backend.backtesting.strategies import STRATEGIES

    runs = db.execute(
        select(BacktestRun).order_by(desc(BacktestRun.created_at)).limit(30)
    ).scalars().all()
    selected = db.get(BacktestRun, run_id) if run_id else (runs[0] if runs else None)
    context = base_context(
        request, db, active="backtesting",
        runs=runs, selected=selected,
        strategies=sorted(STRATEGIES),
        strategy_descriptions={
            name: cls.description for name, cls in STRATEGIES.items()
        },
    )
    return render(request, "backtesting.html", context)


@router.post("/backtesting/run")
def backtesting_run(
    strategy: str = Form(...),
    index: str = Form("egx30"),
    rebalance: str = Form("monthly"),
    initial_capital: float = Form(1_000_000),
    start: str = Form(""),
    top_n: int = Form(10),
    db: Session = Depends(get_db),
) -> Any:
    from backend.backtesting.engine import BacktestConfig, persist_backtest, run_backtest

    start_date = None
    if start.strip():
        try:
            start_date = date.fromisoformat(start.strip())
        except ValueError:
            start_date = None

    config = BacktestConfig(
        strategy=strategy, index=index, rebalance=rebalance,
        initial_capital=initial_capital, start=start_date,
        params={"top_n": top_n},
    )
    result = run_backtest(db, config)
    run = persist_backtest(db, result)
    db.commit()
    return RedirectResponse(f"/backtesting?run_id={run.id}", status_code=303)


# ---------------------------------------------------------------------------
# Reports, evaluation, settings
# ---------------------------------------------------------------------------
@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    report_id: int | None = Query(None),
    q: str | None = Query(None),
    compare: int | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    stmt = select(Report).order_by(desc(Report.created_at))
    if q:
        stmt = stmt.where(Report.markdown.ilike(f"%{q}%"))
    reports = db.execute(stmt.limit(60)).scalars().all()
    selected = db.get(Report, report_id) if report_id else (reports[0] if reports else None)
    compare_report = db.get(Report, compare) if compare else None
    context = base_context(
        request, db, active="reports",
        reports=reports, selected=selected, compare_report=compare_report, q=q or "",
    )
    return render(request, "reports.html", context)


@router.get("/reports/{report_id}/export", response_class=PlainTextResponse)
def report_export(report_id: int, db: Session = Depends(get_db)) -> Any:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "Report not found.")
    return PlainTextResponse(
        report.markdown or "",
        headers={
            "Content-Disposition": (
                f'attachment; filename="egx-alpha-report-{report.period_end}.md"'
            )
        },
    )


@router.post("/reports/generate")
def reports_generate(db: Session = Depends(get_db)) -> Any:
    from backend.api.schemas import uses_synthetic_data
    from backend.reports.weekly import generate_weekly_report

    generate_weekly_report(
        db, acknowledge_synthetic=uses_synthetic_data(db), persist=True
    )
    db.commit()
    return RedirectResponse("/reports", status_code=303)


@router.get("/evaluation", response_class=HTMLResponse)
def evaluation_page(request: Request, db: Session = Depends(get_db)) -> Any:
    from backend.reports.evaluation import evaluate_model

    report = evaluate_model(db)
    db.commit()
    context = base_context(request, db, active="evaluation", report=report)
    return render(request, "evaluation.html", context)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)) -> Any:
    from backend.jobs.scheduler import scheduler_status

    dq = db.execute(
        select(DataQualityLog).order_by(desc(DataQualityLog.created_at)).limit(25)
    ).scalars().all()
    context = base_context(
        request, db, active="settings",
        weights=load_yaml_config("weights"),
        risk_config=load_yaml_config("risk"),
        providers=provider_status(),
        scheduler=scheduler_status(),
        data_quality=dq,
    )
    return render(request, "settings.html", context)
