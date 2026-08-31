#!/usr/bin/env python
"""Populate a demonstration database so GMG can be evaluated end to end.

**Everything this script writes is fictional.** Prices, financial statements,
news and disclosures are generated, and every row is stamped
``SYNTHETIC_DEMO:seed`` so the platform labels it "DEMO DATA — NOT REAL-TIME"
wherever it appears. Company names and tickers are those of real EGX-listed
issuers so the demonstration is recognisable, but **no figure attached to them
is real** and none should be read as a statement about the actual company.

Run it only to explore the product. Delete the database and ingest from a
licensed provider before showing anything to a customer.

    python scripts/seed_gmg_demo.py [--reset]
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("EGX_ALLOW_SYNTHETIC_DATA", "true")

from sqlalchemy import select  # noqa: E402

from backend.core.database import init_database, session_scope  # noqa: E402
from backend.core.logging_config import configure_logging, get_logger  # noqa: E402
from backend.data.models import (  # noqa: E402
    Company,
    Disclosure,
    FinancialStatement,
    NewsItem,
    PriceBar,
)

logger = get_logger(__name__)

SOURCE = "SYNTHETIC_DEMO:seed"

# ticker, name, sector, in_egx30, shares, opening price, revenue EGP, net margin, growth
UNIVERSE: list[tuple[str, str, str, bool, float, float, float, float, float]] = [
    # --- Banks -------------------------------------------------------------
    ("COMI", "Commercial International Bank", "Banks", True, 3.00e9, 88.0, 72.0e9, 0.42, 0.28),
    ("QNBA", "QNB Alahli", "Banks", True, 1.10e9, 62.0, 41.0e9, 0.38, 0.24),
    ("ADIB", "Abu Dhabi Islamic Bank Egypt", "Banks", False, 1.05e9, 27.5, 18.0e9, 0.34, 0.30),
    ("FAIT", "Faisal Islamic Bank of Egypt", "Banks", False, 1.35e9, 12.4, 11.5e9, 0.29, 0.18),
    ("CIEB", "Credit Agricole Egypt", "Banks", False, 3.10e8, 34.0, 9.2e9, 0.36, 0.21),
    # --- Financial services -------------------------------------------------
    ("HRHO", "EFG Holding", "Financial Services", True, 1.25e9, 22.5, 18.5e9, 0.19, 0.31),
    ("CICH", "CI Capital Holding", "Financial Services", False, 5.60e8, 6.9, 4.1e9, 0.15, 0.27),
    ("AMOC", "Amoc Financial Investments", "Financial Services", False, 8.40e8, 9.8, 5.4e9, 0.12, 0.16),
    ("BINV", "B Investments Holding", "Financial Services", False, 2.10e8, 11.2, 1.9e9, 0.22, 0.14),
    # --- Real estate --------------------------------------------------------
    ("TMGH", "Talaat Moustafa Group", "Real Estate", True, 4.10e9, 52.0, 64.0e9, 0.16, 0.35),
    ("PHDC", "Palm Hills Development", "Real Estate", True, 2.30e9, 8.6, 22.0e9, 0.12, 0.29),
    ("MNHD", "Madinet Nasr Housing", "Real Estate", False, 1.55e9, 6.4, 12.5e9, 0.18, 0.22),
    ("HELI", "Heliopolis Housing", "Real Estate", False, 1.28e9, 9.1, 8.2e9, 0.21, 0.17),
    ("OCDI", "SODIC", "Real Estate", False, 5.20e8, 32.0, 16.0e9, 0.14, 0.25),
    # --- Materials ----------------------------------------------------------
    ("ABUK", "Abu Qir Fertilizers", "Materials", True, 1.26e9, 72.0, 26.0e9, 0.32, 0.12),
    ("MFPC", "Misr Fertilizers Production", "Materials", False, 6.00e8, 55.0, 21.0e9, 0.28, 0.10),
    ("SKPC", "Sidi Kerir Petrochemicals", "Materials", True, 5.25e8, 14.8, 9.5e9, 0.14, 0.08),
    ("ESRS", "Ezz Steel", "Materials", True, 5.45e8, 68.0, 84.0e9, 0.06, 0.19),
    ("SVCE", "South Valley Cement", "Materials", False, 8.90e8, 3.1, 3.8e9, 0.05, 0.11),
    # --- Industrials --------------------------------------------------------
    ("SWDY", "Elsewedy Electric", "Industrials", True, 2.15e9, 64.0, 96.0e9, 0.11, 0.24),
    ("ORAS", "Orascom Construction", "Industrials", True, 1.17e8, 148.0, 62.0e9, 0.07, 0.15),
    ("EFIC", "Egyptian Financial & Industrial", "Industrials", False, 2.40e8, 14.6, 4.9e9, 0.09, 0.13),
    ("IRON", "Egyptian Iron & Steel", "Industrials", False, 6.10e8, 2.4, 2.2e9, 0.03, 0.07),
    # --- Consumer staples ---------------------------------------------------
    ("EAST", "Eastern Company", "Consumer Staples", True, 2.25e9, 34.5, 42.0e9, 0.22, 0.19),
    ("JUFO", "Juhayna Food Industries", "Consumer Staples", False, 9.40e8, 19.6, 14.0e9, 0.09, 0.26),
    ("EFID", "Edita Food Industries", "Consumer Staples", False, 7.30e8, 24.8, 11.2e9, 0.10, 0.23),
    ("OLFI", "Obour Land for Food Industries", "Consumer Staples", False, 8.00e8, 8.9, 6.8e9, 0.08, 0.20),
    # --- Consumer discretionary ---------------------------------------------
    ("ORWE", "Oriental Weavers", "Consumer Discretionary", False, 4.50e8, 17.2, 15.5e9, 0.07, 0.14),
    ("GBCO", "GB Corp", "Consumer Discretionary", True, 1.09e9, 12.4, 38.0e9, 0.06, 0.22),
    ("RAYA", "Raya Holding", "Consumer Discretionary", False, 4.10e8, 6.2, 9.4e9, 0.04, 0.18),
    ("DOMT", "Domty", "Consumer Discretionary", False, 6.20e8, 7.8, 7.1e9, 0.06, 0.16),
    # --- Telecommunications & healthcare ------------------------------------
    ("ETEL", "Telecom Egypt", "Telecommunications", True, 1.71e9, 42.0, 58.0e9, 0.15, 0.22),
    ("EFIH", "e-finance for Digital", "Telecommunications", True, 1.68e9, 14.2, 5.6e9, 0.31, 0.27),
    ("FWRY", "Fawry Banking Technology", "Telecommunications", True, 2.20e9, 6.4, 4.2e9, 0.18, 0.34),
    ("ISPH", "Ibnsina Pharma", "Healthcare", False, 1.20e9, 7.4, 32.0e9, 0.025, 0.30),
    ("PHAR", "Egyptian International Pharmaceuticals", "Healthcare", False, 2.60e8, 58.0, 9.8e9, 0.16, 0.18),
    ("CLHO", "Cleopatra Hospitals Group", "Healthcare", True, 1.60e9, 6.8, 8.4e9, 0.15, 0.25),
    ("RMDA", "Tenth of Ramadan Pharmaceuticals", "Healthcare", False, 4.50e8, 4.9, 5.2e9, 0.11, 0.21),
]


#: The benchmark series the weekly report measures the market against. Without
#: it the report correctly refuses to describe a market move, which is honest
#: but leaves the demonstration incomplete.
BENCHMARK = ("EGX30", "EGX 30 Index", 32000.0)


def seed(reset: bool = False) -> None:
    configure_logging()
    init_database()

    with session_scope() as db:
        if reset:
            for model in (PriceBar, FinancialStatement, NewsItem, Disclosure):
                db.query(model).delete()
            logger.info("Cleared existing market data")

        existing = {c.ticker for c in db.execute(select(Company)).scalars().all()}
        if BENCHMARK[0] not in existing:
            db.add(Company(
                ticker=BENCHMARK[0], name=BENCHMARK[1], exchange="EGX",
                currency="EGP", status="INDEX",
                description="Benchmark index series. Demonstration values only.",
            ))
        for ticker, name, sector, egx30, shares, price, revenue, margin, growth in UNIVERSE:
            if ticker in existing:
                continue
            db.add(Company(
                ticker=ticker, name=name, sector=sector, exchange="EGX", currency="EGP",
                status="ACTIVE", in_egx30=egx30, in_egx70=not egx30, in_egx100=True,
                shares_outstanding=shares,
                description=(
                    "Demonstration record. Company identity is real; every figure "
                    "attached to it in this database is generated."
                ),
            ))
        db.flush()

        have_bars = {
            row for row in db.execute(select(PriceBar.ticker).distinct()).scalars().all()
        }

        # --- Prices: three years of daily bars, Sunday to Thursday ----------
        for ticker, name, sector, egx30, shares, price, revenue, margin, growth in UNIVERSE:
            if ticker in have_bars:
                continue
            rng = random.Random(int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16))
            level = price * 0.62
            cursor = date.today() - timedelta(days=1100)
            index = 0
            while cursor < date.today():
                if cursor.weekday() not in (4, 5):   # EGX trades Sunday–Thursday
                    level = max(0.5, level * (1 + 0.00045 + rng.gauss(0, 0.016)))
                    close = round(level * (1 + 0.012 * math.sin(index / 21.0)), 2)
                    high = round(close * (1 + abs(rng.gauss(0, 0.007))), 2)
                    low = round(close * (1 - abs(rng.gauss(0, 0.007))), 2)
                    db.add(PriceBar(
                        ticker=ticker, timestamp=cursor,
                        open=round(low + (high - low) * rng.random(), 2),
                        high=high, low=low, close=close, adjusted_close=close,
                        volume=int(abs(rng.gauss(1.4e6, 6e5))) + 50_000,
                        source=SOURCE,
                    ))
                    index += 1
                cursor += timedelta(days=1)

        # --- Benchmark index series -----------------------------------------
        if BENCHMARK[0] not in have_bars:
            rng = random.Random(4242)
            level = BENCHMARK[2] * 0.7
            cursor = date.today() - timedelta(days=1100)
            while cursor < date.today():
                if cursor.weekday() not in (4, 5):
                    level = max(100.0, level * (1 + 0.0005 + rng.gauss(0, 0.009)))
                    close = round(level, 2)
                    db.add(PriceBar(
                        ticker=BENCHMARK[0], timestamp=cursor,
                        open=round(close * (1 - abs(rng.gauss(0, 0.003))), 2),
                        high=round(close * (1 + abs(rng.gauss(0, 0.004))), 2),
                        low=round(close * (1 - abs(rng.gauss(0, 0.004))), 2),
                        close=close, adjusted_close=close, source=SOURCE,
                    ))
                cursor += timedelta(days=1)

        # --- Financial statements: four annual periods ----------------------
        have_statements = {
            row for row in db.execute(select(FinancialStatement.ticker).distinct()).scalars().all()
        }
        for ticker, name, sector, egx30, shares, price, revenue, margin, growth in UNIVERSE:
            if ticker in have_statements:
                continue
            for back in range(4):
                year = date.today().year - 1 - back
                scaled_revenue = revenue * ((1 + growth) ** -back)
                net = scaled_revenue * margin
                db.add(FinancialStatement(
                    ticker=ticker, period=f"{year}-FY", period_type="FY",
                    period_end=date(year, 12, 31),
                    # Published the following March: the platform will not let
                    # this data influence any calculation dated before then.
                    available_from=date(year + 1, 3, 31),
                    revenue=scaled_revenue,
                    gross_profit=scaled_revenue * (margin + 0.18),
                    ebitda=scaled_revenue * (margin + 0.10),
                    operating_income=scaled_revenue * (margin + 0.05),
                    net_income=net, eps=net / shares,
                    cash=scaled_revenue * 0.22, total_debt=scaled_revenue * 0.35,
                    total_assets=scaled_revenue * 2.4, total_equity=scaled_revenue * 1.1,
                    operating_cash_flow=net * 1.25, capex=-scaled_revenue * 0.07,
                    free_cash_flow=net * 1.25 - scaled_revenue * 0.07,
                    interest_expense=scaled_revenue * 0.03,
                    current_assets=scaled_revenue * 0.9,
                    current_liabilities=scaled_revenue * 0.55,
                    dividends_paid=-net * 0.32,
                    source=SOURCE,
                ))

        # --- News and disclosures -------------------------------------------
        have_news = {
            row for row in db.execute(select(NewsItem.ticker).distinct()).scalars().all()
        }
        for ticker, name, *_rest in UNIVERSE:
            if ticker in have_news:
                continue
            for k in range(3):
                db.add(NewsItem(
                    ticker=ticker,
                    title=f"[DEMO] {name} reports results for the period",
                    news_source=SOURCE, source=SOURCE,
                    url_hash=hashlib.sha256(f"{ticker}-news-{k}".encode()).hexdigest(),
                    publication_date=datetime.combine(
                        date.today() - timedelta(days=6 * k + 2), datetime.min.time()),
                    summary="Generated demonstration item. Not a real news story.",
                ))
                db.add(Disclosure(
                    ticker=ticker,
                    title=f"[DEMO] {name} — board meeting outcome",
                    date=date.today() - timedelta(days=9 * k + 4),
                    disclosure_type="Board", source=SOURCE,
                    url_hash=hashlib.sha256(f"{ticker}-disc-{k}".encode()).hexdigest(),
                    summary="Generated demonstration item. Not a real disclosure.",
                ))

    logger.warning(
        "Demonstration data seeded for %d companies. Every figure is fictional and "
        "is labelled DEMO DATA throughout the application.", len(UNIVERSE),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="delete existing prices, statements, news and disclosures first")
    args = parser.parse_args()
    seed(reset=args.reset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
