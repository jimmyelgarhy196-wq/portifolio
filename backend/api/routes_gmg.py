"""GMG Investment Intelligence: market, stocks, and the stock page.

Access rules applied here:

* The market dashboard, the stock list and a stock's Overview are open to
  signed-in users so the product can be evaluated honestly.
* Analysis that costs research effort — fundamental scoring, valuation, AI
  research, the screener — requires an entitled subscription, checked
  server-side by :func:`require_subscriber` before the template is chosen.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
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
from backend.analytics.valuation import (
    DcfAssumptions,
    blended_valuation,
    multiples_valuation,
    run_dcf,
    sensitivity_grid,
)
from backend.api.auth_deps import (
    Viewer,
    api_require_subscriber,
    get_viewer,
    require_subscriber,
    require_user,
)
from backend.api.deps import data_state, gmg_context, render
from backend.api.routes_auth import flash_from
from backend.core.config import get_settings
from backend.core.database import get_db
from backend.data.models import (
    Company,
    Disclosure,
    NewsItem,
    PriceBar,
    Recommendation,
    ResearchThesis,
    ScoreHistory,
)
from backend.data.saas_models import Quote, UserWatchlist, UserWatchlistItem
from backend.market.overview import (
    INDEX_DEFINITIONS,
    active_universe,
    market_overview,
    search_companies,
)
from backend.market.quotes import get_quotes, provider_chain, quote_freshness
from backend.market.status import market_state
from backend.research.rating import derive_rating

router = APIRouter()

#: Statement lines shown on the Financials tab, in reading order.
STATEMENT_LINES: list[tuple[str, str]] = [
    ("Revenue", "revenue"),
    ("Gross profit", "gross_profit"),
    ("EBITDA", "ebitda"),
    ("Operating income", "operating_income"),
    ("Net income", "net_income"),
    ("Earnings per share", "eps"),
    ("Operating cash flow", "operating_cash_flow"),
    ("Capital expenditure", "capex"),
    ("Free cash flow", "free_cash_flow"),
    ("Dividends paid", "dividends_paid"),
    ("Cash", "cash"),
    ("Total debt", "total_debt"),
    ("Total assets", "total_assets"),
    ("Total equity", "total_equity"),
    ("Current assets", "current_assets"),
    ("Current liabilities", "current_liabilities"),
    ("Interest expense", "interest_expense"),
]

STOCK_TABS: list[tuple[str, str, bool]] = [
    # (slug, label, requires_subscription)
    ("overview", "Overview", False),
    ("chart", "Chart", False),
    ("fundamentals", "Fundamentals", True),
    ("financials", "Financials", True),
    ("valuation", "Valuation", True),
    ("technicals", "Technicals", True),
    ("research", "AI Research", True),
    ("news", "News & Disclosures", False),
    ("peers", "Peers & Sector", True),
    ("data", "Data & Sources", False),
]


def _ctx(request: Request, db: Session, **extra: Any) -> dict[str, Any]:
    context = gmg_context(request, db, **extra)
    context.setdefault("flash", flash_from(request))
    return context


# ---------------------------------------------------------------------------
# Market dashboard
# ---------------------------------------------------------------------------
@router.get("/market", response_class=HTMLResponse)
def market_dashboard(
    request: Request,
    viewer: Viewer = Depends(require_user),
    db: Session = Depends(get_db),
):
    overview = market_overview(db, limit=10)
    db.commit()
    return render(request, "gmg/market.html", _ctx(
        request, db, active="market", overview=overview,
        data_banner={
            "badge": overview.quote_badge,
            "tone": {"DEMO DATA": "demo", "NO DATA": "none", "LIVE": "live"}.get(
                overview.quote_badge, "delayed"),
            "message": overview.data_note,
        },
    ))


# ---------------------------------------------------------------------------
# Stock list
# ---------------------------------------------------------------------------
@router.get("/stocks", response_class=HTMLResponse)
def stocks(
    request: Request,
    q: str = Query(""),
    sector: str = Query(""),
    index: str = Query(""),
    sort: str = Query("ticker"),
    viewer: Viewer = Depends(require_user),
    db: Session = Depends(get_db),
):
    companies = active_universe(db)
    if index in {"EGX30", "EGX70", "EGX100"}:
        attr = {"EGX30": "in_egx30", "EGX70": "in_egx70", "EGX100": "in_egx100"}[index]
        companies = [c for c in companies if getattr(c, attr)]
    if sector:
        companies = [c for c in companies if (c.sector or "") == sector]
    if q:
        needle = q.strip().lower()
        companies = [c for c in companies
                     if needle in c.ticker.lower() or needle in (c.name or "").lower()]

    quotes = get_quotes(db, [c.ticker for c in companies]) if companies else {}
    db.commit()

    rows = []
    for company in companies:
        quote = quotes.get(company.ticker)
        rows.append({
            "company": company, "quote": quote,
            "freshness": quote_freshness(quote),
        })

    reverse = sort in {"change", "volume", "turnover", "price"}

    def key(row: dict[str, Any]) -> Any:
        quote = row["quote"]
        if sort == "change":
            return quote.change_pct if quote and quote.change_pct is not None else -9e9
        if sort == "volume":
            return quote.volume if quote and quote.volume else -1
        if sort == "turnover":
            return quote.turnover if quote and quote.turnover else -1
        if sort == "price":
            return quote.price if quote and quote.price else -1
        if sort == "name":
            return row["company"].name or ""
        return row["company"].ticker

    rows.sort(key=key, reverse=reverse)

    sectors = sorted({c.sector for c in active_universe(db) if c.sector})
    return render(request, "gmg/stocks.html", _ctx(
        request, db, active="stocks", rows=rows, sectors=sectors,
        q=q, sector=sector, index=index, sort=sort,
        data_banner=data_state(db, [c.ticker for c in companies]) if companies else None,
    ))


@router.get("/sectors", response_class=HTMLResponse)
def sectors_page(
    request: Request, viewer: Viewer = Depends(require_user), db: Session = Depends(get_db)
):
    companies = active_universe(db)
    quotes = get_quotes(db, [c.ticker for c in companies]) if companies else {}
    db.commit()

    buckets: dict[str, dict[str, Any]] = {}
    for company in companies:
        name = company.sector or "Unclassified"
        bucket = buckets.setdefault(name, {
            "sector": name, "count": 0, "covered": 0, "advancers": 0, "decliners": 0,
            "turnover": 0.0, "moves": [], "is_demo": False,
        })
        bucket["count"] += 1
        quote = quotes.get(company.ticker)
        if quote is None:
            continue
        bucket["covered"] += 1
        if quote.is_demo:
            bucket["is_demo"] = True
        if quote.change_pct is not None:
            bucket["moves"].append(quote.change_pct)
            if quote.change_pct > 0:
                bucket["advancers"] += 1
            elif quote.change_pct < 0:
                bucket["decliners"] += 1
        if quote.turnover:
            bucket["turnover"] += float(quote.turnover)

    rows = []
    for bucket in buckets.values():
        moves = bucket.pop("moves")
        bucket["average_move"] = sum(moves) / len(moves) if moves else None
        rows.append(bucket)
    rows.sort(key=lambda r: (r["average_move"] is None, -(r["average_move"] or 0)))

    return render(request, "gmg/sectors.html", _ctx(
        request, db, active="sectors", rows=rows,
        data_banner=data_state(db),
    ))


# ---------------------------------------------------------------------------
# Stock page
# ---------------------------------------------------------------------------
def _load_analysis(db: Session, ticker: str):
    settings = get_settings()
    peers = active_universe(db)
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
    return analysis, universe_metrics, sector_map


def _metric(analysis, name: str) -> float | None:
    if analysis.fundamental is None:
        return None
    metric = analysis.fundamental.metrics.get(name)
    return metric.value if metric and metric.available else None


def _valuation_for(db: Session, analysis, universe_metrics, sector_map, price: float | None):
    """Build the valuation view from stored fundamentals only.

    Per-share figures are derived from the price and the company's own guarded
    multiples (EPS = price / (P/E), and so on) rather than from raw statement
    lines. That inherits the scale detection and the plausibility bounds the
    metric engine already applied, so a statement reported in thousands cannot
    produce a valuation a thousand times too large.
    """
    company = analysis.company
    shares = company.shares_outstanding if company else None
    fcf = _metric(analysis, "free_cash_flow")
    net_debt = _metric(analysis, "net_debt") or 0.0
    growth = _metric(analysis, "fcf_growth")
    if growth is None:
        growth = _metric(analysis, "revenue_growth")
    # Cap the starting growth rate: a single strong year is not a forecast.
    if growth is not None:
        growth = max(-0.25, min(growth, 0.30))

    assumptions = DcfAssumptions(
        base_fcf=fcf, shares_outstanding=shares, net_debt=net_debt,
        growth_rate=growth if growth is not None else 0.10,
    )
    dcf = run_dcf(assumptions, current_price=price)

    peer = build_peer_metrics(universe_metrics, sector_map, analysis.ticker)

    def median(values: list[float] | None) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2

    def per_share(multiple_name: str) -> float | None:
        """Invert a guarded multiple back into its per-share figure."""
        multiple = _metric(analysis, multiple_name)
        if price is None or multiple is None or multiple <= 0:
            return None
        return price / multiple

    multiples = multiples_valuation(
        eps=per_share("pe"),
        book_value_per_share=per_share("pb"),
        sales_per_share=per_share("ps"),
        peer_pe=median(peer.get("pe")),
        peer_pb=median(peer.get("pb")),
        peer_ps=median(peer.get("ps")),
        own_pe_median=_own_pe_median(db, analysis.ticker),
    )
    summary = blended_valuation(current_price=price, dcf=dcf, multiples=multiples)
    return summary, assumptions, dcf


def _own_pe_median(db: Session, ticker: str) -> float | None:
    """The company's own median P/E from stored valuation snapshots."""
    from backend.data.models import ValuationSnapshot

    values = [
        row.pe for row in db.execute(
            select(ValuationSnapshot).where(ValuationSnapshot.ticker == ticker)
            .order_by(desc(ValuationSnapshot.date)).limit(24)
        ).scalars().all()
        if row.pe is not None and 0 < row.pe < 100
    ]
    if len(values) < 3:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


@router.get("/stock/{ticker}", response_class=HTMLResponse)
def stock_page(
    request: Request,
    ticker: str,
    tab: str = Query("overview"),
    viewer: Viewer = Depends(require_user),
    db: Session = Depends(get_db),
):
    ticker = ticker.upper()
    company = db.scalar(select(Company).where(Company.ticker == ticker))
    if company is None:
        raise HTTPException(404, f"{ticker} is not an EGX company we cover.")

    tab_slugs = {slug for slug, _label, _paid in STOCK_TABS}
    if tab not in tab_slugs:
        tab = "overview"

    quote = get_quotes(db, [ticker]).get(ticker)
    db.commit()
    freshness = quote_freshness(quote)

    # Tabs behind the paywall are never rendered for a user without entitlement:
    # the server substitutes the upgrade panel rather than hiding content in CSS.
    requires_sub = dict((slug, paid) for slug, _l, paid in STOCK_TABS)[tab]
    locked = requires_sub and not viewer.entitled

    context: dict[str, Any] = {
        "active": "stocks", "company": company, "ticker": ticker,
        "quote": quote, "freshness": freshness, "tab": tab, "tabs": STOCK_TABS,
        "locked": locked,
        "data_banner": {
            "badge": freshness["badge"],
            "tone": {"DEMO DATA": "demo", "NO DATA": "none", "LIVE": "live"}.get(
                freshness["badge"], "delayed"),
            "message": freshness["detail"],
        },
    }

    if not locked:
        analysis, universe_metrics, sector_map = _load_analysis(db, ticker)
        context["analysis"] = analysis
        context["score_delta"] = score_change(db, ticker, as_of=analysis.as_of)

        if tab in {"valuation", "overview"}:
            price = quote.price if quote and quote.price else analysis.price_series.last_close
            summary, assumptions, dcf = _valuation_for(
                db, analysis, universe_metrics, sector_map, price)
            context["valuation"] = summary
            context["dcf"] = dcf
            context["dcf_assumptions"] = assumptions
            if tab == "valuation" and dcf.available:
                context["sensitivity"] = sensitivity_grid(assumptions)

        if tab in {"research", "overview"}:
            context["rating"] = derive_rating(analysis.alpha)

        if tab == "financials":
            from backend.data.models import FinancialStatement

            statements = list(db.execute(
                select(FinancialStatement)
                .where(FinancialStatement.ticker == ticker)
                .order_by(desc(FinancialStatement.period_end)).limit(8)
            ).scalars().all())
            context["periods"] = [
                {
                    "period_label": st.period,
                    "period_end": st.period_end,
                    # Named "lines", not "values": in a Jinja template
                    # ``row.values`` resolves to dict.values() instead.
                    "lines": {field: getattr(st, field) for _label, field in STATEMENT_LINES},
                }
                for st in statements
            ]
            context["statement_lines"] = STATEMENT_LINES

        if tab in {"peers"}:
            peers = [c for c in active_universe(db)
                     if (c.sector or "Unclassified") == (company.sector or "Unclassified")
                     and c.ticker != ticker]
            peer_quotes = get_quotes(db, [c.ticker for c in peers], refresh=False)
            db.commit()
            context["peers"] = [
                {"company": c, "quote": peer_quotes.get(c.ticker),
                 "score": db.scalar(
                     select(ScoreHistory).where(ScoreHistory.ticker == c.ticker)
                     .order_by(desc(ScoreHistory.as_of)))}
                for c in peers
            ]

        if tab == "research":
            thesis = db.scalar(
                select(ResearchThesis).where(ResearchThesis.ticker == ticker)
                .order_by(desc(ResearchThesis.updated_at)))
            context["thesis"] = thesis
            context["recommendation"] = db.scalar(
                select(Recommendation).where(Recommendation.ticker == ticker)
                .order_by(desc(Recommendation.created_at)))
            # Statements carry the agent that produced them and the kind of claim
            # each one is. Grouped, they read as an argument; concatenated, they
            # read as a wall of text.
            grouped: dict[str, list[dict[str, Any]]] = {}
            for statement in (thesis.statements if thesis else []) or []:
                if not isinstance(statement, dict):
                    continue
                grouped.setdefault(statement.get("agent") or "general", []).append(statement)
            context["statements"] = list(grouped.items())

    if tab in {"news", "overview"}:
        context["news"] = list(db.execute(
            select(NewsItem).where(NewsItem.ticker == ticker)
            .order_by(desc(NewsItem.publication_date)).limit(15)
        ).scalars().all())
        context["disclosures"] = list(db.execute(
            select(Disclosure).where(Disclosure.ticker == ticker)
            .order_by(desc(Disclosure.date)).limit(15)
        ).scalars().all())

    if tab == "data":
        context["providers"] = [
            {"name": p.display_name, "is_demo": p.is_demo,
             "available": p.is_available(), "delay": p.delayed_minutes,
             "note": p.status_note()}
            for p in provider_chain(db)
        ]
        context["bar_sources"] = list(db.execute(
            select(PriceBar.source, func.count(), func.max(PriceBar.timestamp))
            .where(PriceBar.ticker == ticker).group_by(PriceBar.source)
        ).all())

    # Watchlist membership, for the star button.
    if viewer.user is not None:
        context["watchlists"] = list(db.execute(
            select(UserWatchlist).where(UserWatchlist.user_id == viewer.user.id)
            .order_by(UserWatchlist.name)
        ).scalars().all())
        context["in_watchlists"] = {
            row.watchlist_id for row in db.execute(
                select(UserWatchlistItem)
                .join(UserWatchlist, UserWatchlist.id == UserWatchlistItem.watchlist_id)
                .where(UserWatchlist.user_id == viewer.user.id,
                       UserWatchlistItem.ticker == ticker)
            ).scalars().all()
        }

    return render(request, "gmg/stock.html", _ctx(request, db, **context))


# ---------------------------------------------------------------------------
# JSON endpoints used by the pages
# ---------------------------------------------------------------------------
@router.get("/api/search")
def api_search(
    request: Request, q: str = Query(""), limit: int = Query(10, ge=1, le=25),
    viewer: Viewer = Depends(get_viewer), db: Session = Depends(get_db),
):
    hits = search_companies(db, q, limit=limit)
    return JSONResponse({"query": q, "results": [h.to_dict() for h in hits]})


@router.get("/api/prices/{ticker}")
def api_prices(
    request: Request, ticker: str, days: int = Query(1300, ge=20, le=6000),
    viewer: Viewer = Depends(require_user), db: Session = Depends(get_db),
):
    """Daily bars for the chart. Carries its own source labelling."""
    ticker = ticker.upper()
    start = date.today() - timedelta(days=days)
    bars = list(db.execute(
        select(PriceBar).where(PriceBar.ticker == ticker, PriceBar.timestamp >= start)
        .order_by(PriceBar.timestamp)
    ).scalars().all())
    sources = sorted({b.source for b in bars if b.source})
    demo = any("SYNTHETIC" in (s or "").upper() for s in sources)
    return JSONResponse({
        "ticker": ticker,
        "bars": [
            {"date": b.timestamp.isoformat(), "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "volume": b.volume}
            for b in bars
        ],
        "sources": sources,
        "is_demo": demo,
        "count": len(bars),
        "note": (
            "Generated demonstration history — not real market data."
            if demo else
            ("Stored daily bars." if bars else "N/A — no price history is stored for this ticker.")
        ),
    })


@router.get("/api/quotes")
def api_quotes(
    request: Request, tickers: str = Query(""),
    viewer: Viewer = Depends(require_user), db: Session = Depends(get_db),
):
    wanted = [t.strip().upper() for t in tickers.split(",") if t.strip()][:60]
    quotes = get_quotes(db, wanted) if wanted else {}
    db.commit()
    return JSONResponse({
        "quotes": {t: {**q.to_dict(), "freshness": quote_freshness(q)}
                   for t, q in quotes.items()},
        "market": market_state().to_dict(),
    })


@router.get("/api/market/overview")
def api_market_overview(
    request: Request, viewer: Viewer = Depends(require_user), db: Session = Depends(get_db)
):
    overview = market_overview(db)
    db.commit()
    return JSONResponse(overview.to_dict())


@router.post("/api/valuation/dcf")
async def api_dcf(
    request: Request, viewer: Viewer = Depends(api_require_subscriber),
    db: Session = Depends(get_db),
):
    """Re-run a DCF with the user's own assumptions.

    Entitlement is checked server-side; the browser cannot reach this by
    editing the page.
    """
    payload = await request.json()
    try:
        assumptions = DcfAssumptions(
            base_fcf=_num(payload.get("base_fcf")),
            shares_outstanding=_num(payload.get("shares_outstanding")),
            net_debt=_num(payload.get("net_debt")) or 0.0,
            growth_rate=_num(payload.get("growth_rate"), 0.10),
            terminal_growth=_num(payload.get("terminal_growth"), 0.05),
            discount_rate=_num(payload.get("discount_rate")),
            risk_free_rate=_num(payload.get("risk_free_rate"), 0.22),
            equity_risk_premium=_num(payload.get("equity_risk_premium"), 0.07),
            beta=_num(payload.get("beta"), 1.0),
            years=int(_num(payload.get("years"), 5) or 5),
        )
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid assumptions.")
    result = run_dcf(assumptions, current_price=_num(payload.get("current_price")))
    return JSONResponse({
        "result": result.to_dict(),
        "sensitivity": sensitivity_grid(assumptions) if result.available else None,
    })


def _num(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
