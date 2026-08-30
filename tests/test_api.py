"""API and page routes."""
from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("EGX_DATABASE_URL", "sqlite:///:memory:")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from backend.api.app import create_app  # noqa: E402
from backend.core.database import get_db  # noqa: E402
from backend.data import models  # noqa: E402
from tests.conftest import make_prices, make_statements


@pytest.fixture
def client():
    """A test client backed by a populated in-memory database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    session = Session()
    session.add(models.Company(
        ticker="TEST", name="Test Company", sector="Banks", exchange="EGX",
        in_egx30=True, in_egx100=True, shares_outstanding=1_000_000_000,
    ))
    session.add(models.Company(
        ticker="PEER", name="Peer Bank", sector="Banks", exchange="EGX",
        in_egx30=True, in_egx100=True, shares_outstanding=800_000_000,
    ))
    session.add(models.Company(ticker="EGX30", name="EGX 30", exchange="EGX", status="INDEX"))
    session.flush()
    for ticker in ("TEST", "PEER"):
        make_prices(session, ticker, days=300, start_price=100.0)
        make_statements(session, ticker, years=4)
    make_prices(session, "EGX30", days=300, start_price=1000.0)
    session.commit()
    session.close()

    app = create_app()

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    engine.dispose()


class TestHealth:
    def test_health_declares_paper_only(self, client):
        payload = client.get("/api/health").json()
        assert payload["status"] == "ok"
        assert payload["mode"] == "PAPER_TRADING_RESEARCH_ONLY"
        assert payload["live_trading"] is False

    def test_universe_reports_verification_state(self, client):
        payload = client.get("/api/universe").json()
        assert payload["status"]["verified"] is False
        assert payload["status"]["warning"]


class TestPages:
    @pytest.mark.parametrize("path", [
        "/", "/markets", "/opportunities", "/stocks", "/stocks/TEST",
        "/theses", "/watchlist", "/portfolio", "/paper-trading", "/risk",
        "/backtesting", "/reports", "/evaluation", "/alerts", "/settings",
        "/research",
    ])
    def test_page_renders(self, client, path):
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        assert "EGX" in response.text

    def test_unknown_ticker_is_404(self, client):
        assert client.get("/stocks/NOSUCH").status_code == 404

    def test_paper_mode_shown_in_header(self, client):
        assert "PAPER" in client.get("/").text


class TestApiEndpoints:
    @pytest.mark.parametrize("path", [
        "/api/health", "/api/providers", "/api/universe", "/api/scores",
        "/api/portfolio", "/api/portfolio/risk", "/api/portfolio/trades",
        "/api/theses", "/api/recommendations", "/api/watchlist", "/api/alerts",
        "/api/backtests", "/api/reports", "/api/evaluation", "/api/data-quality",
        "/api/stocks/TEST", "/api/stocks/TEST/prices", "/api/stocks/TEST/news",
        "/api/stocks/TEST/disclosures",
    ])
    def test_endpoint_returns_json(self, client, path):
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        response.json()

    def test_stock_detail_includes_score_decomposition(self, client):
        payload = client.get("/api/stocks/TEST").json()
        assert payload["ticker"] == "TEST"
        score = payload["alpha"]["score"]
        assert "components" in score
        for component in score["components"]:
            assert {"name", "weight", "value", "available", "contribution"} <= set(component)

    def test_unknown_ticker_404(self, client):
        assert client.get("/api/stocks/NOSUCH").status_code == 404


class TestPaperTradingApi:
    def test_trade_is_marked_paper(self, client):
        response = client.post(
            "/api/portfolio/trade",
            json={"ticker": "TEST", "side": "BUY", "quantity": 100},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["mode"] == "PAPER"
        assert payload["trade"]["mode"] == "PAPER"

    def test_rejected_trade_returns_400_with_reason(self, client):
        response = client.post(
            "/api/portfolio/trade",
            json={"ticker": "TEST", "side": "SELL", "quantity": 999999},
        )
        assert response.status_code == 400
        assert "only" in response.json()["detail"]

    def test_unknown_ticker_rejected(self, client):
        response = client.post(
            "/api/portfolio/trade",
            json={"ticker": "GHOST", "side": "BUY", "quantity": 10},
        )
        assert response.status_code == 400
        assert "No price available" in response.json()["detail"]

    def test_missing_fields_rejected(self, client):
        assert client.post("/api/portfolio/trade", json={"side": "BUY"}).status_code == 400

    def test_sizing_returns_full_reasoning(self, client):
        response = client.post("/api/portfolio/size", json={
            "ticker": "TEST", "conviction": 8.0, "entry_price": 100,
            "target_price": 130, "invalidation_price": 90, "annual_volatility": 0.3,
        })
        assert response.status_code == 200
        payload = response.json()
        assert payload["steps"]
        assert "Recommended allocation" in payload["explanation"]


class TestWatchlistApi:
    def test_add_list_and_remove(self, client):
        created = client.post("/api/watchlist", json={
            "ticker": "TEST", "list_name": "core", "note": "cheap",
        }).json()
        assert created["ticker"] == "TEST"

        listed = client.get("/api/watchlist").json()
        assert "core" in listed

        assert client.delete(f"/api/watchlist/{created['id']}").status_code == 200
        assert client.delete("/api/watchlist/99999").status_code == 404


class TestResearchApi:
    def test_research_produces_a_decision(self, client):
        response = client.post("/api/research/TEST")
        assert response.status_code == 200
        payload = response.json()
        assert payload["decision"]["action"] in ("BUY", "HOLD", "SELL", "WATCH")
        assert payload["evidence"]["items"]
        assert payload["used_llm"] is False

    def test_unknown_ticker_404(self, client):
        assert client.post("/api/research/GHOST").status_code == 404


class TestBacktestApi:
    def test_run_and_retrieve(self, client):
        response = client.post("/api/backtests", json={
            "strategy": "buy_and_hold", "index": "egx30", "rebalance": "quarterly",
            "initial_capital": 1_000_000,
        })
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        detail = client.get(f"/api/backtests/{run_id}").json()
        assert detail["strategy"] == "buy_and_hold"
        assert "metrics" in detail

    def test_bad_date_rejected(self, client):
        response = client.post("/api/backtests", json={
            "strategy": "buy_and_hold", "start": "not-a-date",
        })
        assert response.status_code == 400

    def test_unknown_run_404(self, client):
        assert client.get("/api/backtests/99999").status_code == 404


class TestOpportunityFilters:
    def test_filters_narrow_results(self, client):
        wide = client.get("/opportunities")
        narrow = client.get("/opportunities?min_score=99")
        assert wide.status_code == narrow.status_code == 200
        assert len(narrow.text) <= len(wide.text)

    def test_unknown_metric_excludes_rather_than_passes(self, client):
        # A filter on a metric no name has must return nothing, not everything.
        response = client.get("/opportunities?max_pe=0.0001")
        assert response.status_code == 200
        assert "No names match" in response.text
