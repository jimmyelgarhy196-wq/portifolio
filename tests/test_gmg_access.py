"""Access control: what a visitor, a signed-in user and a subscriber may see.

The requirement these tests defend is explicit: **a user must not be able to
reach premium features by modifying frontend JavaScript.** So the assertions
below never inspect the page for a hidden element — they check that the server
refused to produce the content at all.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.accounts.security import sign_value
from backend.accounts.service import open_session, register_user
from backend.api.app import create_app
from backend.core.config import get_settings
from backend.core.database import get_db
from backend.data import models, saas_models
from backend.data.saas_models import SubscriptionStatus


@pytest.fixture
def app_client():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    session = Session()
    session.add(models.Company(
        ticker="TEST", name="Test Company", sector="Banks", exchange="EGX",
        status="ACTIVE", in_egx30=True, in_egx100=True, shares_outstanding=1_000_000_000,
    ))
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
    with TestClient(app, raise_server_exceptions=False) as client:
        client.session_factory = Session
        yield client
    engine.dispose()


def make_user(client, email: str, *, role: str = "USER",
              status: str | None = None, days: int | None = None) -> str:
    """Create a user, optionally with a subscription, and return a session token."""
    db = client.session_factory()
    try:
        result = register_user(
            db, email=email, password="Harbour-Lantern-91",
            confirm_password="Harbour-Lantern-91",
        )
        assert result.ok and result.user is not None, result.error
        user = result.user
        user.email_verified_at = datetime.now(timezone.utc)
        user.role = role
        if status is not None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            db.add(saas_models.Subscription(
                user_id=user.id, plan_code="gmg-pro",
                plan_name="GMG Investment Intelligence", status=status,
                price_egp=300.0, interval="month",
                current_period_start=now - timedelta(days=1),
                current_period_end=now + timedelta(days=days if days is not None else 30),
            ))
        token = open_session(db, user)
        db.commit()
        return token
    finally:
        db.close()


def sign_in(client, token: str) -> None:
    client.cookies.set(get_settings().session_cookie, sign_value(token))


#: Pages that require a paid subscription, and the JSON endpoints behind them.
PAID_PAGES = ["/screener", "/research", "/reports", "/valuation"]
FREE_PAGES = ["/market", "/stocks", "/sectors", "/watchlists", "/portfolio", "/alerts"]


class TestAnonymousVisitors:
    @pytest.mark.parametrize("path", PAID_PAGES + FREE_PAGES + ["/account", "/admin"])
    def test_application_pages_require_sign_in(self, app_client, path):
        response = app_client.get(path, follow_redirects=False)
        assert response.status_code in (303, 307)
        assert "/login" in response.headers.get("location", "")

    @pytest.mark.parametrize("path", [
        "/", "/pricing", "/about", "/contact", "/data-sources", "/terms",
        "/privacy", "/disclaimer", "/risk-disclosure", "/subscription-terms",
        "/refund-policy", "/cookies", "/login", "/signup",
    ])
    def test_public_pages_are_open(self, app_client, path):
        assert app_client.get(path).status_code == 200

    def test_premium_json_endpoint_is_refused(self, app_client):
        assert app_client.post("/api/valuation/dcf", json={}).status_code == 401


class TestUnsubscribedUsers:
    @pytest.mark.parametrize("path", PAID_PAGES)
    def test_paid_pages_redirect_to_subscription(self, app_client, path):
        sign_in(app_client, make_user(app_client, "plain@example.com"))
        response = app_client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert "/account/subscription" in response.headers["location"]

    @pytest.mark.parametrize("path", FREE_PAGES)
    def test_free_pages_are_available(self, app_client, path):
        sign_in(app_client, make_user(app_client, "plain2@example.com"))
        assert app_client.get(path).status_code == 200

    def test_premium_content_is_never_sent_to_the_browser(self, app_client):
        """The paywalled tab must not be in the HTML at all — not hidden in it."""
        sign_in(app_client, make_user(app_client, "plain3@example.com"))
        page = app_client.get("/stock/TEST?tab=fundamentals")
        assert page.status_code == 200
        body = page.text
        assert "Score breakdown" not in body
        assert "Computed metrics" not in body
        assert "subscription" in body.lower()

    def test_premium_json_endpoint_answers_402(self, app_client):
        sign_in(app_client, make_user(app_client, "plain4@example.com"))
        response = app_client.post("/api/valuation/dcf", json={"base_fcf": 1e9})
        assert response.status_code == 402
        assert "subscription" in response.json()["detail"].lower()


class TestSubscribers:
    @pytest.mark.parametrize("path", PAID_PAGES)
    def test_active_subscription_opens_paid_pages(self, app_client, path):
        sign_in(app_client, make_user(
            app_client, "paid@example.com", status=SubscriptionStatus.ACTIVE.value))
        assert app_client.get(path).status_code == 200

    def test_trial_opens_paid_pages(self, app_client):
        sign_in(app_client, make_user(
            app_client, "trial@example.com", status=SubscriptionStatus.TRIAL.value, days=5))
        assert app_client.get("/screener").status_code == 200

    def test_cancelled_subscription_keeps_paid_time(self, app_client):
        """Cancelling stops renewal; it does not confiscate time already paid for."""
        sign_in(app_client, make_user(
            app_client, "cancelled@example.com",
            status=SubscriptionStatus.CANCELLED.value, days=10))
        assert app_client.get("/screener").status_code == 200

    def test_expired_period_denies_access_whatever_the_status_says(self, app_client):
        """Status alone never grants access: the period end is checked too."""
        sign_in(app_client, make_user(
            app_client, "lapsed@example.com",
            status=SubscriptionStatus.ACTIVE.value, days=-1))
        response = app_client.get("/screener", follow_redirects=False)
        assert response.status_code == 303
        assert "/account/subscription" in response.headers["location"]

    def test_premium_json_endpoint_works(self, app_client):
        sign_in(app_client, make_user(
            app_client, "paid2@example.com", status=SubscriptionStatus.ACTIVE.value))
        response = app_client.post("/api/valuation/dcf", json={
            "base_fcf": 1_000_000_000, "shares_outstanding": 100_000_000,
            "growth_rate": 0.1, "terminal_growth": 0.04, "discount_rate": 0.20,
        })
        assert response.status_code == 200
        assert response.json()["result"]["available"] is True


class TestAdministrators:
    def test_ordinary_user_cannot_reach_the_admin_panel(self, app_client):
        sign_in(app_client, make_user(app_client, "notadmin@example.com"))
        assert app_client.get("/admin/").status_code == 403

    def test_admin_reaches_the_admin_panel(self, app_client):
        sign_in(app_client, make_user(app_client, "boss@example.com", role="ADMIN"))
        assert app_client.get("/admin/").status_code == 200

    def test_admin_is_entitled_without_paying(self, app_client):
        sign_in(app_client, make_user(app_client, "boss2@example.com", role="ADMIN"))
        assert app_client.get("/screener").status_code == 200


class TestSessionHandling:
    def test_a_forged_cookie_is_rejected(self, app_client):
        app_client.cookies.set(get_settings().session_cookie, "not-a-signed-value")
        response = app_client.get("/market", follow_redirects=False)
        assert response.status_code == 303

    def test_an_unsigned_token_is_rejected(self, app_client):
        token = make_user(app_client, "victim@example.com")
        # The raw token without the signature must not authenticate.
        app_client.cookies.set(get_settings().session_cookie, token)
        response = app_client.get("/market", follow_redirects=False)
        assert response.status_code == 303

    def test_signing_out_invalidates_the_session(self, app_client):
        token = make_user(app_client, "leaver@example.com")
        sign_in(app_client, token)
        page = app_client.get("/market")
        assert page.status_code == 200
        import re

        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        app_client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
        # Re-presenting the same token must fail.
        sign_in(app_client, token)
        assert app_client.get("/market", follow_redirects=False).status_code == 303


class TestCsrf:
    def test_state_change_without_a_token_is_refused(self, app_client):
        sign_in(app_client, make_user(app_client, "csrf@example.com"))
        response = app_client.post(
            "/watchlists/create", data={"name": "Injected"}, follow_redirects=False)
        assert response.status_code == 400

    def test_state_change_with_the_token_succeeds(self, app_client):
        sign_in(app_client, make_user(app_client, "csrf2@example.com"))
        import re

        page = app_client.get("/watchlists")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = app_client.post(
            "/watchlists/create", data={"name": "Core", "csrf_token": csrf},
            follow_redirects=False)
        assert response.status_code == 303
        assert "Core" in app_client.get("/watchlists").text


class TestDuplicateNames:
    """Unique constraints must surface as messages, never as a 500."""

    def _csrf(self, client, path="/watchlists"):
        import re

        return re.search(
            r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)

    def test_repeating_a_watchlist_name_is_refused_politely(self, app_client):
        sign_in(app_client, make_user(app_client, "dup@example.com"))
        csrf = self._csrf(app_client)
        first = app_client.post("/watchlists/create",
                                data={"name": "Core", "csrf_token": csrf},
                                follow_redirects=False)
        assert first.status_code == 303
        second = app_client.post("/watchlists/create",
                                 data={"name": "Core", "csrf_token": csrf},
                                 follow_redirects=False)
        assert second.status_code == 303, "a repeated name must not raise"
        assert "watchlist_exists" in second.headers["location"]
        assert "already have a watchlist with that name" in app_client.get(
            "/watchlists?msg=watchlist_exists").text

    def test_an_empty_watchlist_name_is_refused(self, app_client):
        sign_in(app_client, make_user(app_client, "dup2@example.com"))
        response = app_client.post(
            "/watchlists/create", data={"name": "   ", "csrf_token": self._csrf(app_client)},
            follow_redirects=False)
        assert response.status_code == 303
        assert "watchlist_name_required" in response.headers["location"]

    def test_adding_the_same_ticker_twice_toggles_it_off(self, app_client):
        sign_in(app_client, make_user(app_client, "dup3@example.com"))
        csrf = self._csrf(app_client)
        app_client.post("/watchlists/create", data={"name": "Core", "csrf_token": csrf})
        db = app_client.session_factory()
        watchlist_id = db.query(saas_models.UserWatchlist).one().id
        db.close()

        for _ in range(2):
            response = app_client.post(
                f"/watchlists/{watchlist_id}/toggle",
                data={"ticker": "TEST", "csrf_token": csrf}, follow_redirects=False)
            assert response.status_code == 303

        db = app_client.session_factory()
        assert db.query(saas_models.UserWatchlistItem).count() == 0
        db.close()

    def test_saving_a_screen_twice_updates_it(self, app_client):
        sign_in(app_client, make_user(
            app_client, "dup4@example.com", status=SubscriptionStatus.ACTIVE.value))
        csrf = self._csrf(app_client, "/screener")
        for payload in ('{"criteria": []}', '{"criteria": [{"key": "pe", "op": "lte", "value": 10}]}'):
            response = app_client.post(
                "/screener/save",
                data={"name": "Cheap", "payload": payload, "csrf_token": csrf},
                follow_redirects=False)
            assert response.status_code == 303

        db = app_client.session_factory()
        screens = db.query(saas_models.SavedScreen).all()
        assert len(screens) == 1
        assert screens[0].filters["criteria"], "the second save must update, not duplicate"
        db.close()


class TestUserDataIsolation:
    def test_one_user_cannot_reach_another_users_watchlist(self, app_client):
        import re

        owner_token = make_user(app_client, "owner@example.com")
        sign_in(app_client, owner_token)
        page = app_client.get("/watchlists")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        app_client.post("/watchlists/create", data={"name": "Private", "csrf_token": csrf})

        db = app_client.session_factory()
        watchlist_id = db.query(saas_models.UserWatchlist).one().id
        db.close()

        intruder_token = make_user(app_client, "intruder@example.com")
        sign_in(app_client, intruder_token)
        page = app_client.get("/watchlists")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = app_client.post(
            f"/watchlists/{watchlist_id}/delete", data={"csrf_token": csrf},
            follow_redirects=False)
        assert response.status_code == 404
        # And it still exists.
        db = app_client.session_factory()
        assert db.query(saas_models.UserWatchlist).count() == 1
        db.close()
