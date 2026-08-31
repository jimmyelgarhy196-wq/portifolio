"""The subscriber workspace: screener, watchlists, portfolio, alerts, research.

Every route here calls :func:`require_subscriber` or :func:`require_user`, and
every query is scoped to the signed-in user's own rows. A watchlist, portfolio
or alert belonging to someone else is not reachable by guessing its id: the
lookup itself is filtered by ``user_id``, so a wrong id is simply not found.

The portfolio is a **tracking** tool. It records what the user tells it they
own so it can compute value and P/L. GMG places no orders, holds no securities
and holds no money.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from backend.analytics.service import build_universe_metrics
from backend.analytics.screener import (
    FILTER_BY_KEY,
    FILTER_GROUPS,
    FILTERS,
    run_screen,
)
from backend.api.auth_deps import (
    Viewer,
    client_ip,
    enforce_csrf,
    require_subscriber,
    require_user,
)
from backend.api.deps import data_state, gmg_context, render
from backend.api.routes_auth import flash_from
from backend.core.database import get_db
from backend.data.models import Company, Recommendation, Report, ScoreHistory
from backend.data.saas_models import (
    SavedScreen,
    UserAlert,
    UserPortfolio,
    UserPosition,
    UserWatchlist,
    UserWatchlistItem,
)
from backend.market.overview import active_universe
from backend.market.quotes import get_quotes, quote_freshness
from backend.notify.user_alerts import CONDITIONS, NEEDS_THRESHOLD, evaluate_alert

router = APIRouter()


def _ctx(request: Request, db: Session, **extra: Any) -> dict[str, Any]:
    context = gmg_context(request, db, **extra)
    context.setdefault("flash", flash_from(request))
    return context


def _latest_scores(db: Session) -> dict[str, ScoreHistory]:
    latest = (
        select(ScoreHistory.ticker, func.max(ScoreHistory.as_of).label("as_of"))
        .group_by(ScoreHistory.ticker).subquery()
    )
    rows = db.execute(
        select(ScoreHistory).join(
            latest,
            (ScoreHistory.ticker == latest.c.ticker) & (ScoreHistory.as_of == latest.c.as_of),
        )
    ).scalars().all()
    return {row.ticker: row for row in rows}


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------
@router.get("/screener", response_class=HTMLResponse)
def screener(
    request: Request, screen: int | None = Query(None),
    sort: str = Query("alpha_score"),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_subscriber),
):
    saved = list(db.execute(
        select(SavedScreen).where(SavedScreen.user_id == viewer.user.id)
        .order_by(SavedScreen.name)
    ).scalars().all())

    criteria: list[dict[str, Any]] = []
    sectors: list[str] = []
    indices: list[str] = []
    loaded: SavedScreen | None = None

    if screen is not None:
        loaded = db.scalar(select(SavedScreen).where(
            SavedScreen.id == screen, SavedScreen.user_id == viewer.user.id))
        if loaded is not None:
            filters = loaded.filters or {}
            criteria = filters.get("criteria", [])
            sectors = filters.get("sectors", [])
            indices = filters.get("indices", [])
            sort = filters.get("sort", sort)
    else:
        criteria = _criteria_from_query(request)
        sectors = request.query_params.getlist("sector")
        indices = request.query_params.getlist("index")

    companies = active_universe(db)
    tickers = [c.ticker for c in companies]
    quotes = get_quotes(db, tickers) if companies else {}
    db.commit()
    # Fundamental and quant values, so the screener can filter on more than the
    # quote cache. Without this every fundamental column reads N/A.
    metrics = build_universe_metrics(db, tickers) if tickers else {}
    result = run_screen(
        db, companies=companies, quotes=quotes, scores=_latest_scores(db),
        metrics=metrics, criteria=criteria, sectors=sectors, indices=indices,
        sort_key=sort, descending=True, limit=150,
    )

    return render(request, "gmg/screener.html", _ctx(
        request, db, active="screener", result=result, filters=FILTERS,
        filter_groups=FILTER_GROUPS, filter_by_key=FILTER_BY_KEY,
        criteria=criteria, sectors=sectors, indices=indices, sort=sort,
        saved=saved, loaded=loaded,
        all_sectors=sorted({c.sector for c in companies if c.sector}),
        data_banner=data_state(db),
    ))


def _criteria_from_query(request: Request) -> list[dict[str, Any]]:
    """Read ``min_<key>`` / ``max_<key>`` query parameters into criteria."""
    criteria: list[dict[str, Any]] = []
    for key in FILTER_BY_KEY:
        for prefix, op in (("min_", "gte"), ("max_", "lte")):
            raw = request.query_params.get(prefix + key)
            if raw in (None, ""):
                continue
            try:
                criteria.append({"key": key, "op": op, "value": float(raw)})
            except ValueError:
                continue
    return criteria


@router.post("/screener/save")
def save_screen(
    request: Request, csrf_token: str = Form(""), name: str = Form(""),
    payload: str = Form(""), db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_subscriber),
):
    enforce_csrf(request, csrf_token)
    import json

    name = (name or "").strip()[:96]
    if not name:
        return RedirectResponse("/screener", status_code=303)
    try:
        filters = json.loads(payload) if payload else {}
    except ValueError:
        filters = {}

    existing = db.scalar(select(SavedScreen).where(
        SavedScreen.user_id == viewer.user.id, SavedScreen.name == name))
    if existing is None:
        existing = SavedScreen(user_id=viewer.user.id, name=name)
        db.add(existing)
    existing.filters = filters
    db.commit()
    return RedirectResponse(f"/screener?screen={existing.id}", status_code=303)


@router.post("/screener/{screen_id}/delete")
def delete_screen(
    request: Request, screen_id: int, csrf_token: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_subscriber),
):
    enforce_csrf(request, csrf_token)
    row = db.scalar(select(SavedScreen).where(
        SavedScreen.id == screen_id, SavedScreen.user_id == viewer.user.id))
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse("/screener", status_code=303)


# ---------------------------------------------------------------------------
# Watchlists
# ---------------------------------------------------------------------------
@router.get("/watchlists", response_class=HTMLResponse)
def watchlists(
    request: Request, db: Session = Depends(get_db), viewer: Viewer = Depends(require_user)
):
    lists = list(db.execute(
        select(UserWatchlist).where(UserWatchlist.user_id == viewer.user.id)
        .order_by(UserWatchlist.created_at)
    ).scalars().all())
    tickers = sorted({item.ticker for wl in lists for item in wl.items})
    quotes = get_quotes(db, tickers) if tickers else {}
    db.commit()
    names = {
        c.ticker: c.name for c in db.execute(
            select(Company).where(Company.ticker.in_(tickers))
        ).scalars().all()
    } if tickers else {}

    return render(request, "gmg/watchlists.html", _ctx(
        request, db, active="watchlists", lists=lists, quotes=quotes, names=names,
        freshness={t: quote_freshness(q) for t, q in quotes.items()},
        data_banner=data_state(db, tickers) if tickers else None,
    ))


@router.post("/watchlists/create")
def create_watchlist(
    request: Request, csrf_token: str = Form(""), name: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    name = (name or "").strip()[:96]
    if not name:
        return RedirectResponse("/watchlists?msg=watchlist_name_required", status_code=303)

    # Watchlist names are unique per user. Check first so a repeated name is a
    # message, not a 500.
    existing = db.scalar(select(UserWatchlist).where(
        UserWatchlist.user_id == viewer.user.id, UserWatchlist.name == name))
    if existing is not None:
        return RedirectResponse("/watchlists?msg=watchlist_exists", status_code=303)

    db.add(UserWatchlist(user_id=viewer.user.id, name=name))
    db.commit()
    return RedirectResponse("/watchlists?msg=watchlist_created", status_code=303)


def _own_watchlist(db: Session, viewer: Viewer, watchlist_id: int) -> UserWatchlist:
    row = db.scalar(select(UserWatchlist).where(
        UserWatchlist.id == watchlist_id, UserWatchlist.user_id == viewer.user.id))
    if row is None:
        raise HTTPException(404, "That watchlist does not exist.")
    return row


@router.post("/watchlists/{watchlist_id}/toggle")
def toggle_watchlist_item(
    request: Request, watchlist_id: int, csrf_token: str = Form(""),
    ticker: str = Form(""), db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    watchlist = _own_watchlist(db, viewer, watchlist_id)
    ticker = (ticker or "").strip().upper()[:24]
    existing = db.scalar(select(UserWatchlistItem).where(
        UserWatchlistItem.watchlist_id == watchlist.id,
        UserWatchlistItem.ticker == ticker))
    if existing is not None:
        db.delete(existing)
    elif ticker:
        db.add(UserWatchlistItem(watchlist_id=watchlist.id, ticker=ticker))
    db.commit()
    referer = request.headers.get("referer", "")
    target = referer if referer.startswith("/") else f"/stock/{ticker}"
    return RedirectResponse(target, status_code=303)


@router.post("/watchlists/{watchlist_id}/delete")
def delete_watchlist(
    request: Request, watchlist_id: int, csrf_token: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    db.delete(_own_watchlist(db, viewer, watchlist_id))
    db.commit()
    return RedirectResponse("/watchlists", status_code=303)


# ---------------------------------------------------------------------------
# Portfolio (tracking only)
# ---------------------------------------------------------------------------
def _portfolio_for(db: Session, viewer: Viewer) -> UserPortfolio:
    row = db.scalar(select(UserPortfolio).where(UserPortfolio.user_id == viewer.user.id)
                    .order_by(UserPortfolio.created_at))
    if row is None:
        row = UserPortfolio(user_id=viewer.user.id, name="My portfolio", currency="EGP")
        db.add(row)
        db.flush()
    return row


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio(
    request: Request, db: Session = Depends(get_db), viewer: Viewer = Depends(require_user)
):
    book = _portfolio_for(db, viewer)
    positions = list(book.positions)
    tickers = sorted({p.ticker for p in positions})
    quotes = get_quotes(db, tickers) if tickers else {}
    db.commit()

    rows: list[dict[str, Any]] = []
    total_cost = 0.0
    total_value = 0.0
    valued = 0
    demo = False
    for position in positions:
        quote = quotes.get(position.ticker)
        cost = position.shares * position.purchase_price
        total_cost += cost
        price = quote.price if quote else None
        value = position.shares * price if price is not None else None
        if value is not None:
            total_value += value
            valued += 1
        if quote is not None and quote.is_demo:
            demo = True
        rows.append({
            "position": position, "quote": quote, "cost": cost, "value": value,
            "pnl": (value - cost) if value is not None else None,
            "pnl_pct": ((value - cost) / cost) if (value is not None and cost) else None,
            "freshness": quote_freshness(quote),
        })

    for row in rows:
        row["weight"] = (row["value"] / total_value) if (row["value"] and total_value) else None

    unvalued = len(positions) - valued
    summary = {
        "positions": len(positions),
        "total_cost": total_cost if positions else None,
        "total_value": total_value if valued else None,
        "pnl": (total_value - total_cost) if valued == len(positions) and positions else None,
        "pnl_pct": (
            (total_value - total_cost) / total_cost
            if (valued == len(positions) and positions and total_cost) else None
        ),
        "valued": valued, "unvalued": unvalued, "is_demo": demo,
        # Partial valuation is stated rather than presented as a total.
        "partial": unvalued > 0,
    }

    return render(request, "gmg/portfolio.html", _ctx(
        request, db, active="portfolio", book=book, rows=rows, summary=summary,
        data_banner=data_state(db, tickers) if tickers else None,
    ))


@router.post("/portfolio/positions/add")
def add_position(
    request: Request, csrf_token: str = Form(""), ticker: str = Form(""),
    shares: str = Form(""), purchase_price: str = Form(""),
    purchase_date: str = Form(""), note: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    book = _portfolio_for(db, viewer)
    try:
        share_count = float(shares)
        price = float(purchase_price)
    except (TypeError, ValueError):
        return RedirectResponse("/portfolio?msg=invalid_position", status_code=303)
    if share_count <= 0 or price <= 0:
        return RedirectResponse("/portfolio?msg=invalid_position", status_code=303)

    bought_on: date | None = None
    if purchase_date:
        try:
            bought_on = date.fromisoformat(purchase_date)
        except ValueError:
            bought_on = None

    db.add(UserPosition(
        portfolio_id=book.id, ticker=ticker.strip().upper()[:24],
        shares=share_count, purchase_price=price, purchase_date=bought_on,
        note=(note or "").strip()[:500] or None,
    ))
    db.commit()
    return RedirectResponse("/portfolio", status_code=303)


@router.post("/portfolio/positions/{position_id}/delete")
def delete_position(
    request: Request, position_id: int, csrf_token: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    row = db.scalar(
        select(UserPosition).join(UserPortfolio, UserPortfolio.id == UserPosition.portfolio_id)
        .where(UserPosition.id == position_id, UserPortfolio.user_id == viewer.user.id)
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse("/portfolio", status_code=303)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
@router.get("/alerts", response_class=HTMLResponse)
def alerts(
    request: Request, db: Session = Depends(get_db), viewer: Viewer = Depends(require_user)
):
    rows = list(db.execute(
        select(UserAlert).where(UserAlert.user_id == viewer.user.id)
        .order_by(desc(UserAlert.created_at))
    ).scalars().all())
    quotes = get_quotes(db, sorted({a.ticker for a in rows}), refresh=False) if rows else {}
    db.commit()
    evaluations = {a.id: evaluate_alert(db, a, quotes.get(a.ticker)) for a in rows}

    return render(request, "gmg/alerts.html", _ctx(
        request, db, active="alerts", alerts=rows, conditions=CONDITIONS,
        needs_threshold=sorted(NEEDS_THRESHOLD), evaluations=evaluations,
        quotes=quotes,
    ))


@router.post("/alerts/create")
def create_alert(
    request: Request, csrf_token: str = Form(""), ticker: str = Form(""),
    condition: str = Form(""), threshold: str = Form(""),
    email_delivery: str = Form(""), note: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    if condition not in CONDITIONS:
        return RedirectResponse("/alerts", status_code=303)
    value: float | None = None
    if threshold:
        try:
            value = float(threshold)
        except ValueError:
            value = None
    if condition in NEEDS_THRESHOLD and value is None:
        return RedirectResponse("/alerts?msg=alert_needs_threshold", status_code=303)

    db.add(UserAlert(
        user_id=viewer.user.id, ticker=ticker.strip().upper()[:24],
        condition=condition, threshold=value,
        email_delivery=bool(email_delivery), note=(note or "").strip()[:500] or None,
    ))
    db.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{alert_id}/toggle")
def toggle_alert(
    request: Request, alert_id: int, csrf_token: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    row = db.scalar(select(UserAlert).where(
        UserAlert.id == alert_id, UserAlert.user_id == viewer.user.id))
    if row is not None:
        row.active = not row.active
        db.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{alert_id}/delete")
def delete_alert(
    request: Request, alert_id: int, csrf_token: str = Form(""),
    db: Session = Depends(get_db), viewer: Viewer = Depends(require_user),
):
    enforce_csrf(request, csrf_token)
    row = db.scalar(select(UserAlert).where(
        UserAlert.id == alert_id, UserAlert.user_id == viewer.user.id))
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse("/alerts", status_code=303)


# ---------------------------------------------------------------------------
# Research and reports
# ---------------------------------------------------------------------------
@router.get("/research", response_class=HTMLResponse)
def research(
    request: Request, db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_subscriber),
):
    scores = _latest_scores(db)
    companies = {c.ticker: c for c in active_universe(db)}
    ranked = sorted(
        (s for s in scores.values() if s.alpha_score is not None and s.ticker in companies),
        key=lambda s: s.alpha_score, reverse=True,
    )
    quotes = get_quotes(db, [s.ticker for s in ranked[:40]], refresh=False) if ranked else {}
    db.commit()
    recommendations = list(db.execute(
        select(Recommendation).order_by(desc(Recommendation.created_at)).limit(25)
    ).scalars().all())

    return render(request, "gmg/research.html", _ctx(
        request, db, active="research", ranked=ranked[:40], companies=companies,
        quotes=quotes, recommendations=recommendations,
        unscored=len(companies) - len(ranked),
        data_banner=data_state(db),
    ))


@router.get("/reports", response_class=HTMLResponse)
def reports(
    request: Request, db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_subscriber),
):
    rows = list(db.execute(
        select(Report).order_by(desc(Report.created_at)).limit(20)
    ).scalars().all())
    return render(request, "gmg/reports.html", _ctx(
        request, db, active="reports", reports=rows,
    ))


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def report_detail(
    request: Request, report_id: int, db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_subscriber),
):
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "That report does not exist.")
    return render(request, "gmg/report_detail.html", _ctx(
        request, db, active="reports", report=report,
    ))


@router.get("/valuation", response_class=HTMLResponse)
def valuation_tools(
    request: Request, ticker: str = Query(""), db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_subscriber),
):
    companies = active_universe(db)
    return render(request, "gmg/valuation_tools.html", _ctx(
        request, db, active="valuation", companies=companies, ticker=ticker.upper(),
    ))
