"""Export a full data snapshot as JSON.

Originally fed the standalone HTML terminal now kept as
``legacy-terminal-snapshot.html`` — a frozen, read-only export of an earlier
build. The current product is the running web application; this script remains
useful for extracting a complete computed snapshot for analysis or archiving.

Everything it emits carries the provenance stored with it, so a consumer can
still tell demonstration data from real data.

    python scripts/export_snapshot.py > snapshot.json
"""
import sys, json, logging
sys.path.insert(0, '/home/user/portifolio')

# Silence all logging first: the app logs to stdout, which would corrupt the JSON.
logging.disable(logging.CRITICAL)
import backend.core.logging_config as lc
lc.configure_logging()
for h in logging.getLogger().handlers:
    h.setStream(sys.stderr) if hasattr(h, "setStream") else None

from datetime import date
from sqlalchemy import select, desc, func
from backend.core.database import session_scope
from backend.data.models import *
from backend.analytics.service import (analyze_stock, build_universe_metrics,
    build_peer_metrics, load_price_series, score_change)
from backend.analytics.quant import analyze_universe
from backend.analytics.indicators import sma, rsi, macd
from backend.data.universe import get_universe, universe_status
from backend.portfolio.paper_trading import get_or_create_portfolio, mark_to_market
from backend.portfolio.risk import analyze_risk
from backend.portfolio.attribution import analyze_attribution
from backend.reports.evaluation import evaluate_model

TECH_FIELDS = ("sma20","sma50","sma100","sma200","ema20","ema50","rsi14","macd",
               "macd_signal","macd_hist","atr14","atr_pct","bb_position","volume",
               "volume_ratio","volatility_20d","momentum_1m","momentum_3m",
               "momentum_6m","momentum_12m","relative_strength_3m")

def r(x, n=3):
    return None if x is None else round(x, n)

out = {}
with session_scope() as s:
    comps = get_universe(s, "all")
    sector_map = {c.ticker: c.sector or "Unknown" for c in comps}
    um = build_universe_metrics(s, [c.ticker for c in comps])
    quant = analyze_universe(um)
    bench = load_price_series(s, "EGX30")
    detail = {c.ticker for c in get_universe(s, "egx30")}

    out["meta"] = {"generated": date.today().isoformat(),
                   "universe": universe_status(s).to_dict(),
                   "synthetic": True, "ai_enabled": False}

    stocks = {}
    for c in comps:
        a = analyze_stock(s, c.ticker, quant_snapshot=quant.get(c.ticker),
                          peer_metrics=build_peer_metrics(um, sector_map, c.ticker),
                          benchmark_series=bench if len(bench) else None)
        t, f, al = a.technical, a.fundamental, a.alpha
        row = {
            "ticker": c.ticker, "name": c.name, "sector": c.sector,
            "in30": c.in_egx30, "in70": c.in_egx70, "shares": c.shares_outstanding,
            "price": r(a.price_series.last_close), "asOf": a.as_of.isoformat(),
            "score": r(al.value, 1), "confidence": al.score.confidence.value,
            "coverage": r(al.score.coverage, 3),
            "sub": {k: r(v, 1) for k, v in {
                "fundamental": al.fundamental.value if al.fundamental else None,
                "technical": al.technical.value if al.technical else None,
                "quant": al.quant.value if al.quant else None,
                "catalyst": al.catalyst.value if al.catalyst else None,
                "risk": al.risk.value if al.risk else None,
                "quality": al.quality_value, "sentiment": al.sentiment_value,
            }.items()},
            "breakdown": al.score.breakdown(),
            "warnings": al.warnings,
            "trend": t.trend if t else None,
            "metrics": {k: {"v": (m.value if m.available else None),
                            "f": m.formatted(), "note": m.note}
                        for k, m in (f.metrics.items() if f and not f.insufficient_data else [])},
            "peers": f.peer_context if f else {},
            "period": f.latest_period if f else None,
            "src": f.source if f else None,
            "fnote": f.note if f else None,
            "tech": ({k: r(getattr(t, k), 4) for k in TECH_FIELDS}
                     if t and not t.insufficient_data else {}),
            "support": [r(x, 2) for x in (t.support_levels if t else [])],
            "resistance": [r(x, 2) for x in (t.resistance_levels if t else [])],
            "signals": [sg.to_dict() for sg in (t.signals if t else [])],
            "factors": ({k: {"score": r(v.score, 1), "cov": r(v.coverage, 2)}
                         for k, v in quant[c.ticker].factors.items()}
                        if c.ticker in quant else {}),
            "delta": score_change(s, c.ticker, as_of=a.as_of),
        }
        if c.ticker in detail:
            ps = a.price_series
            closes = ps.closes[-260:]
            ml, sl, hh = macd(closes)
            row["series"] = {
                "d": [d.isoformat()[2:] for d in ps.dates[-260:]],
                "c": [r(x, 3) for x in closes],
                "v": [r(x, 0) for x in ps.volumes[-260:]],
                "sma20": [r(x, 2) for x in sma(closes, 20)],
                "sma50": [r(x, 2) for x in sma(closes, 50)],
                "rsi": [r(x, 1) for x in rsi(closes, 14)],
                "macd": [r(x, 3) for x in ml],
                "macdSig": [r(x, 3) for x in sl],
                "macdHist": [r(x, 3) for x in hh],
            }
        stocks[c.ticker] = row
    out["stocks"] = stocks
    out["benchmark"] = {"ticker": "EGX30",
                        "d": [d.isoformat()[2:] for d in bench.dates[-260:]],
                        "c": [r(x, 2) for x in bench.closes[-260:]]}

    out["theses"] = [t.to_dict() for t in s.execute(
        select(ResearchThesis).order_by(desc(ResearchThesis.alpha_score))).scalars().all()]

    p = get_or_create_portfolio(s)
    st = mark_to_market(s, p)
    out["portfolio"] = {
        "name": p.name, "mode": p.mode, "currency": p.currency,
        "initial": p.initial_capital, "cash": st["cash"], "value": st["total_value"],
        "cashWeight": st["cash_weight"], "gross": st["gross_exposure"],
        "net": st["net_exposure"], "unrealized": st["unrealized_pnl"],
        "realized": st["realized_pnl"], "totalReturn": st["total_return"],
        "positions": [x.to_dict() for x in st["positions"]],
        "unpriced": st["unpriced_tickers"]}
    out["risk"] = analyze_risk(s, p).to_dict()
    out["attribution"] = analyze_attribution(s, p).to_dict()
    out["equity"] = [{"d": x.as_of.isoformat(), "v": r(x.total_value, 2), "b": r(x.benchmark_value, 2)}
                     for x in s.execute(select(PortfolioSnapshot)
                        .order_by(PortfolioSnapshot.as_of)).scalars().all()]
    out["trades"] = [t.to_dict() for t in s.execute(
        select(Trade).order_by(desc(Trade.executed_at)).limit(60)).scalars().all()]
    out["watchlist"] = [w.to_dict() for w in s.execute(select(WatchlistItem)).scalars().all()]
    out["alerts"] = [a.to_dict() for a in s.execute(
        select(Alert).order_by(desc(Alert.created_at)).limit(40)).scalars().all()]
    out["dq"] = [d.to_dict() for d in s.execute(
        select(DataQualityLog).order_by(desc(DataQualityLog.created_at)).limit(40)).scalars().all()]
    out["reports"] = [{**x.to_dict(), "markdown": x.markdown} for x in s.execute(
        select(Report).order_by(desc(Report.created_at))).scalars().all()]
    out["backtests"] = [{**b.to_dict(),
                         "equity": b.equity_curve[::max(1, len(b.equity_curve)//170)],
                         "tradeSample": b.trades[:30]}
                        for b in s.execute(select(BacktestRun).order_by(desc(BacktestRun.id))).scalars().all()]
    out["evaluation"] = evaluate_model(s).to_dict()
    out["news"] = [n.to_dict() for n in s.execute(
        select(NewsItem).order_by(desc(NewsItem.publication_date)).limit(160)).scalars().all()]
    out["disclosures"] = [d.to_dict() for d in s.execute(
        select(Disclosure).order_by(desc(Disclosure.date)).limit(160)).scalars().all()]
    out["counts"] = {t.__tablename__: s.scalar(select(func.count()).select_from(t))
        for t in (Company, PriceBar, FinancialStatement, ValuationSnapshot, NewsItem,
                  Disclosure, ResearchThesis, Recommendation, ScoreHistory, Report,
                  BacktestRun, DataQualityLog)}

def enc(o):
    from datetime import date as D, datetime as DT
    return o.isoformat() if isinstance(o, (D, DT)) else str(o)

sys.stdout.write(json.dumps(out, default=enc, separators=(",", ":")))
