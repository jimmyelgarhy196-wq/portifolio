#!/usr/bin/env python3
"""Seed the database with SYNTHETIC DEMO data.

⚠  EVERYTHING THIS SCRIPT LOADS IS FICTIONAL.

It exists so the terminal can be explored without a data subscription, and so
the pipeline can be exercised offline. Every row is stamped SYNTHETIC_DEMO, the
UI shows a permanent warning banner, and reports refuse to generate without an
explicit acknowledgement.

Requires EGX_ALLOW_SYNTHETIC_DATA=true.

    EGX_ALLOW_SYNTHETIC_DATA=true python scripts/seed_demo.py
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402
from backend.core.database import init_database, session_scope  # noqa: E402
from backend.core.logging_config import configure_logging, get_logger  # noqa: E402
from backend.data.ingestion import (  # noqa: E402
    compute_valuation_snapshot,
    ingest_fundamentals,
    ingest_prices,
)
from backend.data.models import Company, Disclosure, NewsItem  # noqa: E402
from backend.data.providers.egx_disclosure import classify_disclosure, url_hash  # noqa: E402
from backend.data.providers.registry import ProviderChain  # noqa: E402
from backend.data.providers.rss_news import score_sentiment  # noqa: E402
from backend.data.universe import get_universe, load_universe  # noqa: E402

logger = get_logger("seed_demo")

# Fictional headline templates. Company names are inserted at render time; the
# events themselves never happened.
DEMO_EVENTS = [
    ("{name} reports FY results with revenue up {pct}%", "EARNINGS", 5),
    ("{name} board proposes cash dividend of EGP {amt} per share", "DIVIDEND", 4),
    ("{name} announces EGP {amt}bn expansion programme", "CONTRACT", 4),
    ("{name} completes acquisition of a regional subsidiary", "M&A", 5),
    ("{name} approves share buyback programme", "BUYBACK", 4),
    ("{name} appoints new Chief Financial Officer", "MANAGEMENT_CHANGE", 3),
    ("{name} secures major infrastructure contract", "CONTRACT", 4),
    ("{name} announces capital increase to fund growth", "CAPITAL_ACTION", 4),
    ("{name} margins compress on higher input costs", "EARNINGS", 4),
    ("{name} delays project completion to next quarter", "OTHER", 3),
]


def seed_events(session, companies, rng: random.Random, count_per_company: int = 3) -> int:
    """Insert fictional disclosures and news, clearly stamped as synthetic."""
    inserted = 0
    today = date.today()
    for company in companies:
        for _ in range(rng.randint(0, count_per_company)):
            template, kind, importance = rng.choice(DEMO_EVENTS)
            title = template.format(
                name=company.name.split("(")[0].strip(),
                pct=rng.randint(4, 38),
                amt=rng.choice([0.5, 1.0, 1.5, 2.0, 2.5, 3.0]),
            )
            event_date = today - timedelta(days=rng.randint(1, 150))
            digest = url_hash("synthetic", title, event_date, company.ticker)
            if session.query(Disclosure).filter_by(url_hash=digest).first():
                continue
            detected_kind, detected_importance = classify_disclosure(title)
            session.add(Disclosure(
                ticker=company.ticker, title=title, date=event_date,
                disclosure_type=detected_kind or kind, url=None, url_hash=digest,
                summary="SYNTHETIC DEMO EVENT — this disclosure is fictional.",
                importance=detected_importance or importance,
                source="SYNTHETIC_DEMO:seed", retrieved_at=datetime.now(timezone.utc),
                confidence="UNVERIFIED",
            ))
            sentiment, label = score_sentiment(title)
            session.add(NewsItem(
                ticker=company.ticker, title=title, news_source="SYNTHETIC_DEMO",
                url=None, url_hash=url_hash("synthetic-news", title, event_date, company.ticker),
                publication_date=datetime(event_date.year, event_date.month, event_date.day),
                summary="SYNTHETIC DEMO NEWS — this article is fictional.",
                sentiment=sentiment, sentiment_label=label, importance=importance,
                source="SYNTHETIC_DEMO:seed", retrieved_at=datetime.now(timezone.utc),
                confidence="UNVERIFIED",
            ))
            inserted += 1
    session.flush()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default="all", help="egx30 | egx70 | egx100 | all")
    parser.add_argument("--lookback-days", type=int, default=1100)
    parser.add_argument("--seed", type=int, default=20240101)
    parser.add_argument("--reset", action="store_true", help="drop and recreate all tables")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    if not settings.allow_synthetic_data:
        print(
            "REFUSED: synthetic data is disabled.\n\n"
            "This script loads FICTIONAL data for offline demonstration only.\n"
            "To proceed, run:\n\n"
            "    EGX_ALLOW_SYNTHETIC_DATA=true python scripts/seed_demo.py\n",
            file=sys.stderr,
        )
        return 2

    print("=" * 74)
    print("  ⚠  SEEDING SYNTHETIC DEMO DATA — ALL FIGURES ARE FICTIONAL")
    print("     No price, financial statement or event corresponds to a real")
    print("     security. Do not act on anything this dataset produces.")
    print("=" * 74)

    from backend.data.providers.synthetic import SyntheticProvider

    init_database(drop_all=args.reset)
    rng = random.Random(args.seed)
    provider = SyntheticProvider(force=True, seed=args.seed)

    with session_scope() as session:
        load_universe(session)

    with session_scope() as session:
        companies = get_universe(session, args.index, include_benchmarks=True)
        tickers = [c.ticker for c in companies]

        # Share counts are needed for market cap and every valuation multiple.
        for company in companies:
            if company.status != "INDEX" and not company.shares_outstanding:
                company.shares_outstanding = round(
                    random.Random(f"shares:{company.ticker}").uniform(80e6, 2.5e9), 0
                )

        print(f"\nIngesting synthetic prices for {len(tickers)} instruments...")
        price_summary = ingest_prices(
            session, tickers=tickers, chain=ProviderChain([provider], "prices"),
            lookback_days=args.lookback_days,
        )
        print(f"  prices: +{price_summary.inserted} bars")

        equities = [c for c in companies if c.status != "INDEX"]
        print(f"Ingesting synthetic fundamentals for {len(equities)} companies...")
        fundamental_summary = ingest_fundamentals(
            session, tickers=[c.ticker for c in equities],
            chain=ProviderChain([provider], "fundamentals"),
        )
        print(f"  fundamentals: +{fundamental_summary.inserted} statements")

        print("Generating synthetic disclosures and news...")
        events = seed_events(session, equities, rng)
        print(f"  events: +{events} disclosures and matching news items")

    # Valuation snapshots are derived from what was just stored.
    with session_scope() as session:
        equities = [c for c in get_universe(session, args.index)]
        made = 0
        for company in equities:
            # A short history of snapshots lets the engine compare a company
            # against its own past valuation, not just against peers.
            for offset in (0, 90, 180, 365, 545):
                snapshot_date = date.today() - timedelta(days=offset)
                if compute_valuation_snapshot(session, company.ticker, snapshot_date):
                    made += 1
        print(f"  valuation snapshots: {made}")

    print("\nDone. Start the terminal with:")
    print("    EGX_ALLOW_SYNTHETIC_DATA=true python scripts/run_server.py\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
