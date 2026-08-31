"""Weekly report, alerts, evaluation and the weekly pipeline."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.data.models import Alert, Disclosure, Recommendation, ResearchThesis, ScoreHistory
from backend.portfolio.paper_trading import execute_trade, get_or_create_portfolio
from backend.reports.alerts import (
    check_high_conviction,
    check_price_targets,
    check_risk_limits,
    check_score_changes,
    create_alert,
    dispatch_notifications,
    run_all_checks,
)
from backend.reports.evaluation import evaluate_model, grade_recommendations
from backend.reports.weekly import SyntheticDataRefused, generate_weekly_report
from tests.conftest import make_prices, make_statements


@pytest.fixture
def market(db, company, benchmark):
    make_prices(db, "TEST", days=300, start_price=100.0)
    make_prices(db, "EGX30", days=300, start_price=1000.0)
    make_statements(db, "TEST", years=4)
    return company


class TestWeeklyReport:
    def test_has_all_ten_sections(self, db, market):
        report = generate_weekly_report(db, persist=False)
        assert len(report.sections) == 10
        assert [s.key for s in report.sections] == [
            "executive_summary", "performance", "winners", "losers",
            "opportunities", "positions", "thesis_changes", "risk",
            "macro", "next_week",
        ]

    def test_markdown_renders(self, db, market):
        markdown = generate_weekly_report(db, persist=False).to_markdown()
        assert markdown.startswith("# EGX ALPHA Weekly Investment Committee")
        for i in range(1, 11):
            assert f"## {i}." in markdown
        assert "not investment advice" in markdown

    def test_macro_section_refuses_to_fabricate(self, db, market):
        section = generate_weekly_report(db, persist=False).section("macro")
        assert "no macroeconomic data feed" in section.body
        assert "would mean inventing them" in section.body
        assert "MacroDataProvider" in section.body

    def test_movers_do_not_invent_causes(self, db, market):
        section = generate_weekly_report(db, persist=False).section("winners")
        assert "not determinable" in section.body or "No disclosure on file" in section.body

    def test_mover_cause_cited_when_disclosure_exists(self, db, market):
        db.add(Disclosure(
            ticker="TEST", title="Board approves large cash dividend",
            date=date.today() - timedelta(days=2), disclosure_type="DIVIDEND",
            url_hash="h1", importance=5, source="test",
            retrieved_at=datetime.now(timezone.utc),
        ))
        db.flush()
        report = generate_weekly_report(db, persist=False)
        combined = report.section("winners").body + report.section("losers").body
        assert "DIVIDEND" in combined

    def test_refuses_synthetic_without_acknowledgement(self, db, company, benchmark):
        make_prices(db, "TEST", days=200, source="SYNTHETIC_DEMO:synthetic")
        with pytest.raises(SyntheticDataRefused):
            generate_weekly_report(db, persist=False)

    def test_synthetic_marked_when_acknowledged(self, db, company, benchmark):
        make_prices(db, "TEST", days=200, source="SYNTHETIC_DEMO:synthetic")
        report = generate_weekly_report(db, acknowledge_synthetic=True, persist=False)
        assert report.contains_synthetic
        assert "SYNTHETIC DEMONSTRATION DATA" in report.to_markdown()

    def test_persisted_report_is_retrievable(self, db, market):
        from backend.data.models import Report

        generate_weekly_report(db, persist=True)
        stored = db.scalar(select(Report))
        assert stored is not None and stored.markdown
        assert stored.sections

    def test_position_actions_are_justified(self, db, market):
        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=100,
                      strategy="fundamental_long")
        section = generate_weekly_report(db, persist=False).section("positions")
        assert "TEST" in section.body
        assert any(word in section.body for word in ("HOLD", "ADD", "REDUCE", "EXIT"))

    def test_breadth_commentary_matches_index_direction(self, db, market):
        body = generate_weekly_report(db, persist=False).section("executive_summary").body
        # Anchor on the benchmark sentence itself. The word "declined" also
        # appears in the breadth count ("N advanced and M declined"), which says
        # nothing about the index's own direction.
        fell = "benchmark declined" in body
        rose = "benchmark gained" in body
        assert fell or rose, "the benchmark move must be stated"
        # The commentary must never claim the opposite of the measured move.
        if fell:
            assert "The index rose" not in body
        if rose:
            assert "The index fell" not in body


class TestAlerts:
    def test_price_target_alert(self, db, market):
        db.add(ResearchThesis(
            reference="EGX-00001", ticker="TEST", direction="LONG",
            strategy="fundamental_long", target_price=1.0, status="ACTIVE",
        ))
        db.flush()
        alerts = check_price_targets(db)
        assert any(a.alert_type == "PRICE_TARGET" for a in alerts)

    def test_invalidation_alert_and_status_change(self, db, market):
        thesis = ResearchThesis(
            reference="EGX-00002", ticker="TEST", direction="LONG",
            strategy="fundamental_long", invalidation_price=1_000_000.0, status="ACTIVE",
        )
        db.add(thesis)
        db.flush()
        alerts = check_price_targets(db)
        assert any(a.alert_type == "THESIS_INVALIDATED" for a in alerts)
        assert thesis.status == "INVALIDATED"

    def test_score_change_alert(self, db, market):
        today = date.today()
        db.add(ScoreHistory(ticker="TEST", as_of=today - timedelta(days=7), alpha_score=60.0))
        db.add(ScoreHistory(ticker="TEST", as_of=today, alpha_score=78.0))
        db.flush()
        alerts = check_score_changes(db, as_of=today)
        assert any(a.alert_type == "SCORE_CHANGE" for a in alerts)
        assert "18 points" in alerts[0].title

    def test_small_score_move_is_not_alerted(self, db, market):
        today = date.today()
        db.add(ScoreHistory(ticker="TEST", as_of=today - timedelta(days=7), alpha_score=60.0))
        db.add(ScoreHistory(ticker="TEST", as_of=today, alpha_score=62.0))
        db.flush()
        assert not check_score_changes(db, as_of=today)

    def test_high_conviction_alert_skips_held_names(self, db, market):
        today = date.today()
        db.add(ScoreHistory(ticker="TEST", as_of=today, alpha_score=85.0))
        db.flush()
        assert check_high_conviction(db, as_of=today)

        portfolio = get_or_create_portfolio(db)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=10)
        db.query(Alert).delete()
        db.flush()
        assert not check_high_conviction(db, as_of=today)

    def test_deduplication(self, db):
        first = create_alert(db, alert_type="TEST", title="Same", message="m")
        second = create_alert(db, alert_type="TEST", title="Same", message="m")
        assert first is not None and second is None

    def test_risk_limit_alerts(self, db, market):
        from backend.portfolio.paper_trading import latest_price

        portfolio = get_or_create_portfolio(db)
        # Size the trade from the actual price so the 20% position limit is
        # genuinely breached regardless of where the fixture series ended up.
        price = latest_price(db, "TEST")
        quantity = int((portfolio.cash * 0.45) / price)
        execute_trade(db, portfolio, ticker="TEST", side="BUY", quantity=quantity,
                      strategy="fundamental_long")
        alerts = check_risk_limits(db)
        codes = {a.payload.get("code") for a in alerts}
        assert "POSITION_CONCENTRATION" in codes

    def test_run_all_checks(self, db, market):
        run_all_checks(db)   # must not raise on a sparse database

    def test_notifications_off_by_default(self, db):
        create_alert(db, alert_type="TEST", title="x", message="y")
        result = dispatch_notifications(db)
        assert result.sent == 0
        assert "disabled" in result.skipped_reason


class TestEvaluation:
    def _recommendation(self, days_ago: int, **overrides):
        payload = dict(
            ticker="TEST", action="BUY", direction="LONG",
            strategy="fundamental_long", sector="Banks",
            price_at_reco=100.0, conviction=8.0, alpha_score=75.0,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=days_ago),
        )
        payload.update(overrides)
        return Recommendation(**payload)

    def test_no_recommendations_says_so(self, db):
        report = evaluate_model(db)
        assert report.total_recommendations == 0
        assert any("No recommendations" in note for note in report.notes)

    def test_refuses_win_rate_before_horizon(self, db, market):
        db.add(self._recommendation(days_ago=5))
        db.flush()
        report = evaluate_model(db)
        assert report.graded == 0
        assert any("horizon" in note for note in report.notes)
        assert any("measuring noise" in note for note in report.notes)

    def test_grades_after_horizon(self, db, market):
        db.add(self._recommendation(days_ago=200))
        db.flush()
        assert grade_recommendations(db) == 1
        recommendation = db.scalar(select(Recommendation))
        assert recommendation.outcome_status == "GRADED"
        assert recommendation.realized_return is not None
        assert recommendation.holding_days is not None

    def test_short_call_is_the_negation_of_a_long(self, db, market):
        """A SHORT is graded on the decline it anticipated."""
        db.add(self._recommendation(days_ago=200, action="BUY", direction="LONG"))
        db.add(self._recommendation(days_ago=200, action="SELL", direction="SHORT"))
        db.flush()
        grade_recommendations(db)
        rows = db.execute(select(Recommendation)).scalars().all()
        long_return = next(r.realized_return for r in rows if r.direction == "LONG")
        short_return = next(r.realized_return for r in rows if r.direction == "SHORT")
        assert short_return == pytest.approx(-long_return)
        assert long_return != 0

    def test_missing_price_history_is_marked(self, db):
        db.add(self._recommendation(days_ago=200, ticker="GHOST"))
        db.flush()
        grade_recommendations(db)
        assert db.scalar(select(Recommendation)).outcome_status == "NO_DATA"

    def test_breakdowns_populated(self, db, market):
        for i in range(4):
            db.add(self._recommendation(days_ago=200 + i))
        db.flush()
        report = evaluate_model(db)
        assert report.by_strategy and report.by_action
        assert report.by_conviction and report.by_score_range
        assert report.overall.graded == 4

    def test_benchmark_comparison_recorded(self, db, market):
        db.add(self._recommendation(days_ago=200))
        db.flush()
        grade_recommendations(db)
        assert db.scalar(select(Recommendation)).benchmark_return is not None


class TestWeeklyPipeline:
    def test_runs_all_steps_and_isolates_failures(self, db, market, monkeypatch):
        from backend.jobs import weekly as weekly_module

        # The pipeline opens its own sessions; point them at the test database.
        from contextlib import contextmanager

        @contextmanager
        def fake_scope():
            yield db

        monkeypatch.setattr(weekly_module, "session_scope", fake_scope)
        result = weekly_module.run_weekly_pipeline(
            index="egx30", skip_ingestion=True, research_limit=2,
            acknowledge_synthetic=True,
        )
        assert len(result.steps) == 11
        names = [s.name for s in result.steps]
        assert "Generate and store weekly report" in names
        assert "Recalculate scores" in names
        # Rendering must work whatever the outcome of individual steps.
        assert "EGX ALPHA weekly run" in result.render()
