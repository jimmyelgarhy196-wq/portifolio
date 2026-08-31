"""Shared pytest fixtures.

Every test runs against a fresh in-memory database, so tests never touch the
user's real data and cannot leak state into one another.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

# Must be set before any application module reads configuration.
os.environ["EGX_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["EGX_ALLOW_SYNTHETIC_DATA"] = "true"
os.environ["EGX_SCHEDULER_ENABLED"] = "false"
os.environ["EGX_NOTIFICATIONS_ENABLED"] = "false"
os.environ.pop("ANTHROPIC_API_KEY", None)

from backend.core.config import get_settings, reload_configs  # noqa: E402
from backend.core.database import get_engine, reset_engine  # noqa: E402
from backend.data import models  # noqa: E402
from backend.data import saas_models  # noqa: E402,F401  (registers SaaS tables)


@pytest.fixture(scope="function")
def db():
    """A fresh in-memory database session per test."""
    reset_engine()
    reload_configs()
    os.environ["EGX_DATABASE_URL"] = "sqlite:///:memory:"

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def company(db):
    c = models.Company(
        ticker="TEST", name="Test Company", sector="Banks", exchange="EGX",
        currency="EGP", status="ACTIVE", in_egx30=True, in_egx100=True,
        shares_outstanding=1_000_000_000,
    )
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def benchmark(db):
    c = models.Company(
        ticker="EGX30", name="EGX 30 Index", exchange="EGX", status="INDEX",
    )
    db.add(c)
    db.flush()
    return c


def make_prices(
    db, ticker: str, *, days: int = 300, start_price: float = 100.0,
    daily_drift: float = 0.0005, end: date | None = None, source: str = "test",
) -> list[models.PriceBar]:
    """Insert a deterministic price series. Sunday-Thursday, EGX-style."""
    end = end or date.today()
    bars: list[models.PriceBar] = []
    price = start_price
    current = end - timedelta(days=int(days * 1.45))
    index = 0
    while current <= end:
        if current.weekday() in (4, 5):   # Friday, Saturday: EGX closed
            current += timedelta(days=1)
            continue
        # Deterministic wobble, no RNG, so tests are reproducible. It cycles on
        # the bar index rather than the calendar ordinal: keying off the ordinal
        # while skipping Fri/Sat samples the cycle non-uniformly and injects a
        # drift the caller did not ask for.
        wobble = 0.011 * ((index % 6) - 2.5) / 2.5
        index += 1
        price = max(1.0, price * (1.0 + daily_drift + wobble))
        bar = models.PriceBar(
            ticker=ticker, timestamp=current,
            open=round(price * 0.995, 3), high=round(price * 1.012, 3),
            low=round(price * 0.988, 3), close=round(price, 3),
            adjusted_close=round(price, 3), volume=100_000 + (current.toordinal() % 50) * 1000,
            source=source, retrieved_at=datetime.now(timezone.utc), confidence="HIGH",
        )
        db.add(bar)
        bars.append(bar)
        current += timedelta(days=1)
    db.flush()
    return bars


def make_statements(
    db, ticker: str, *, years: int = 4, base_revenue: float = 10_000_000_000.0,
    growth: float = 0.12, margin: float = 0.15, source: str = "test",
) -> list[models.FinancialStatement]:
    """Insert annual statements with realistic publication lags."""
    out: list[models.FinancialStatement] = []
    this_year = date.today().year
    revenue = base_revenue
    for offset in range(years, 0, -1):
        year = this_year - offset
        revenue *= 1.0 + growth
        net_income = revenue * margin
        equity = revenue * 0.9
        period_end = date(year, 12, 31)
        statement = models.FinancialStatement(
            ticker=ticker, period=f"{year}-FY", period_type="FY",
            period_end=period_end,
            available_from=period_end + timedelta(days=90),
            revenue=revenue, gross_profit=revenue * 0.42, ebitda=revenue * 0.28,
            operating_income=revenue * 0.24, net_income=net_income,
            eps=net_income / 1_000_000_000, cash=revenue * 0.1,
            total_debt=equity * 0.4, total_assets=equity * 1.8, total_equity=equity,
            operating_cash_flow=net_income * 1.15, capex=revenue * 0.05,
            free_cash_flow=net_income * 1.15 - revenue * 0.05,
            interest_expense=revenue * 0.02,
            current_assets=revenue * 0.5, current_liabilities=revenue * 0.25,
            dividends_paid=net_income * 0.3,
            source=source, retrieved_at=datetime.now(timezone.utc), confidence="HIGH",
        )
        db.add(statement)
        out.append(statement)
    db.flush()
    return out
