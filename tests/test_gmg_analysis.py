"""Valuation, screening, rating and alert logic.

The shared theme: when the data does not support an answer, the system must say
so rather than produce a plausible-looking number.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.analytics.screener import FILTER_BY_KEY, run_screen
from backend.analytics.valuation import (
    MAX_METHOD_DISPERSION,
    DcfAssumptions,
    MultipleValuation,
    blended_valuation,
    multiples_valuation,
    run_dcf,
    sensitivity_grid,
)
from backend.analytics.scoring import Confidence, ScoreComponent, ScoreResult
from backend.data import models, saas_models
from backend.notify.user_alerts import evaluate_alert
from backend.research.rating import MIN_COVERAGE_FOR_RATING, derive_rating


def assumptions(**kwargs) -> DcfAssumptions:
    base = dict(base_fcf=1_000_000_000.0, shares_outstanding=500_000_000.0,
                net_debt=0.0, growth_rate=0.10, terminal_growth=0.04,
                discount_rate=0.20, years=5)
    base.update(kwargs)
    return DcfAssumptions(**base)


class TestDcf:
    def test_produces_a_value_from_complete_inputs(self):
        result = run_dcf(assumptions())
        assert result.available
        assert result.fair_value_per_share > 0
        assert len(result.projections) == 5

    def test_present_values_decline_with_the_discount_factor(self):
        projections = run_dcf(assumptions()).projections
        factors = [p["discount_factor"] for p in projections]
        assert factors == sorted(factors, reverse=True)

    def test_growth_fades_toward_the_terminal_rate(self):
        projections = run_dcf(assumptions(growth_rate=0.30, terminal_growth=0.04)).projections
        assert projections[0]["growth"] > projections[-1]["growth"]
        assert abs(projections[-1]["growth"] - 0.04) < 1e-9

    def test_refuses_without_free_cash_flow(self):
        result = run_dcf(assumptions(base_fcf=None))
        assert not result.available
        assert "N/A" in result.unavailable_reason

    def test_refuses_without_a_share_count(self):
        result = run_dcf(assumptions(shares_outstanding=None))
        assert not result.available
        assert "share count" in result.unavailable_reason

    def test_refuses_on_negative_cash_flow_instead_of_returning_a_negative_value(self):
        result = run_dcf(assumptions(base_fcf=-5_000_000.0))
        assert not result.available
        assert "not generating cash" in result.unavailable_reason

    def test_refuses_when_terminal_growth_reaches_the_discount_rate(self):
        """Otherwise the terminal value is infinite or negative."""
        result = run_dcf(assumptions(terminal_growth=0.20, discount_rate=0.20))
        assert not result.available
        assert "infinite" in result.unavailable_reason

    def test_warns_when_the_terminal_value_dominates(self):
        result = run_dcf(assumptions(growth_rate=0.02, terminal_growth=0.10,
                                     discount_rate=0.11, years=3))
        assert result.available
        assert any("terminal value" in note for note in result.notes)

    def test_capm_is_used_when_no_discount_rate_is_supplied(self):
        result = run_dcf(assumptions(discount_rate=None, risk_free_rate=0.20,
                                     equity_risk_premium=0.06, beta=1.5))
        assert result.assumptions["discount_rate_source"] == "CAPM"
        assert result.assumptions["discount_rate"] == pytest.approx(0.29)

    def test_sensitivity_grid_re_runs_the_model(self):
        grid = sensitivity_grid(assumptions())
        assert len(grid["rows"]) == 5
        assert all(len(row["values"]) == 5 for row in grid["rows"])
        # A higher discount rate must give a lower value.
        first_col = [row["values"][0] for row in grid["rows"] if row["values"][0]]
        assert first_col == sorted(first_col, reverse=True)


class TestMultiples:
    def test_unavailable_inputs_yield_unavailable_rows(self):
        rows = multiples_valuation(eps=None, peer_pe=8.0)
        assert rows[0].available is False
        assert "N/A" in rows[0].unavailable_reason

    def test_negative_earnings_are_not_meaningful(self):
        rows = multiples_valuation(eps=-2.0, peer_pe=8.0)
        assert rows[0].available is False
        assert "not positive" in rows[0].unavailable_reason

    def test_valid_inputs_multiply_out(self):
        rows = multiples_valuation(eps=5.0, peer_pe=10.0)
        assert rows[0].fair_value == 50.0


class TestBlendedValuation:
    def _dcf(self, value: float | None):
        return run_dcf(assumptions()) if value else run_dcf(assumptions(base_fcf=None))

    def test_reports_when_nothing_could_be_computed(self):
        summary = blended_valuation(current_price=10.0, dcf=self._dcf(None), multiples=[])
        assert summary.fair_value is None
        assert summary.method_count == 0
        assert "N/A" in summary.note

    def test_single_method_is_flagged_as_such(self):
        summary = blended_valuation(current_price=10.0, dcf=self._dcf(1), multiples=[])
        assert summary.method_count == 1
        assert "Only one valuation method" in summary.note

    def test_wide_disagreement_withholds_the_headline_number(self):
        """Averaging methods that differ six-fold invents precision."""
        multiples = [
            MultipleValuation("a", "Sector P/E", 5.0, "x"),
            MultipleValuation("b", "Sector P/B", 40.0, "x"),
        ]
        summary = blended_valuation(
            current_price=10.0, dcf=self._dcf(None), multiples=multiples)
        assert summary.withheld is True
        assert summary.fair_value is None
        assert summary.low == 5.0 and summary.high == 40.0
        assert summary.dispersion > MAX_METHOD_DISPERSION

    def test_close_agreement_publishes_a_value(self):
        multiples = [
            MultipleValuation("a", "Sector P/E", 20.0, "x"),
            MultipleValuation("b", "Sector P/B", 22.0, "x"),
        ]
        summary = blended_valuation(
            current_price=10.0, dcf=self._dcf(None), multiples=multiples)
        assert summary.withheld is False
        assert 20.0 <= summary.fair_value <= 22.0

    def test_weights_are_redistributed_across_working_methods(self):
        multiples = [MultipleValuation("a", "Sector P/E", 21.0, "x")]
        summary = blended_valuation(
            current_price=10.0, dcf=self._dcf(1), multiples=multiples)
        weights = [m["weight"] for m in summary.methods if m["available"]]
        assert sum(weights) == pytest.approx(1.0)


def make_alpha(*, total, coverage=1.0, fundamental=None, technical=None,
               confidence=Confidence.HIGH, staleness=(), warnings=()):
    """A minimal AlphaScore stand-in for the rating rules."""
    from backend.analytics.master_score import AlphaScore

    def result(value):
        if value is None:
            return None
        return ScoreResult(name="sub", value=value,
                           components=[ScoreComponent("x", 100, value)])

    score = ScoreResult(
        name="alpha", value=total, confidence=confidence, coverage=coverage,
        components=[ScoreComponent("x", 100, total)],
    )
    return AlphaScore(
        ticker="TEST", as_of=date.today(), score=score,
        fundamental=result(fundamental), technical=result(technical),
        staleness=list(staleness), warnings=list(warnings),
    )


class TestRating:
    def test_withheld_when_there_is_no_score(self):
        rating = derive_rating(make_alpha(total=None))
        assert not rating.available
        assert "N/A" in rating.unavailable_reason

    def test_withheld_below_the_coverage_floor(self):
        rating = derive_rating(make_alpha(total=80.0, coverage=MIN_COVERAGE_FOR_RATING - 0.1))
        assert not rating.available
        assert "withheld" in rating.unavailable_reason.lower()

    @pytest.mark.parametrize("score,expected", [
        (92.0, "STRONG_BUY"), (70.0, "BUY"), (50.0, "HOLD"),
        (35.0, "REDUCE"), (20.0, "SELL"),
    ])
    def test_bands(self, score, expected):
        assert derive_rating(make_alpha(total=score, fundamental=score)).code == expected

    def test_fundamental_lead_implies_a_long_horizon(self):
        rating = derive_rating(make_alpha(total=75.0, fundamental=85.0, technical=50.0))
        assert rating.horizon == "LONG"

    def test_technical_lead_implies_a_short_horizon(self):
        rating = derive_rating(make_alpha(total=75.0, fundamental=50.0, technical=85.0))
        assert rating.horizon == "SHORT"

    def test_both_strong_is_hybrid(self):
        rating = derive_rating(make_alpha(total=80.0, fundamental=78.0, technical=76.0))
        assert rating.category == "HYBRID"

    def test_missing_technical_data_is_stated_as_a_caveat(self):
        rating = derive_rating(make_alpha(total=75.0, fundamental=80.0))
        assert any("technical" in c.lower() for c in rating.caveats)

    def test_confidence_is_never_raised_above_the_score_confidence(self):
        rating = derive_rating(make_alpha(
            total=80.0, fundamental=80.0, technical=80.0, confidence=Confidence.LOW))
        assert rating.confidence == "LOW"

    def test_thin_coverage_lowers_confidence(self):
        rating = derive_rating(make_alpha(
            total=80.0, coverage=0.55, fundamental=80.0, technical=80.0))
        assert rating.confidence in {"LOW", "MEDIUM"}
        assert any("coverage" in c.lower() for c in rating.caveats)


@pytest.fixture
def screen_setup(db):
    companies = []
    for ticker, sector, egx30 in [("AAA", "Banks", True), ("BBB", "Banks", True),
                                  ("CCC", "Materials", False)]:
        company = models.Company(
            ticker=ticker, name=f"{ticker} Co", sector=sector, exchange="EGX",
            status="ACTIVE", in_egx30=egx30, in_egx70=not egx30, in_egx100=True,
            shares_outstanding=1e9,
        )
        db.add(company)
        companies.append(company)
    db.flush()
    quotes = {
        "AAA": saas_models.Quote(ticker="AAA", price=10.0, change_pct=0.02, volume=1e6,
                                 turnover=1e7, is_demo=False),
        "BBB": saas_models.Quote(ticker="BBB", price=20.0, change_pct=-0.01, volume=2e6,
                                 turnover=4e7, is_demo=False),
        "CCC": saas_models.Quote(ticker="CCC", price=5.0, change_pct=0.00, volume=5e5,
                                 turnover=2.5e6, is_demo=False),
    }
    metrics = {
        "AAA": {"pe": 8.0, "roe": 0.25},
        "BBB": {"pe": 20.0, "roe": 0.10},
        "CCC": {"roe": 0.15},          # no P/E: cannot be tested on it
    }
    return companies, quotes, metrics


class TestScreener:
    def test_no_criteria_returns_the_whole_universe(self, db, screen_setup):
        companies, quotes, metrics = screen_setup
        result = run_screen(db, companies=companies, quotes=quotes, scores={},
                            metrics=metrics)
        assert len(result.rows) == 3

    def test_a_criterion_narrows_the_result(self, db, screen_setup):
        companies, quotes, metrics = screen_setup
        result = run_screen(db, companies=companies, quotes=quotes, scores={},
                            metrics=metrics,
                            criteria=[{"key": "pe", "op": "lte", "value": 10.0}])
        assert {row.ticker for row in result.rows} == {"AAA"}

    def test_unknown_values_exclude_rather_than_silently_pass(self, db, screen_setup):
        """CCC has no P/E. It must not be treated as cheap."""
        companies, quotes, metrics = screen_setup
        result = run_screen(db, companies=companies, quotes=quotes, scores={},
                            metrics=metrics,
                            criteria=[{"key": "pe", "op": "lte", "value": 100.0}])
        assert "CCC" not in {row.ticker for row in result.rows}
        assert result.excluded_for_missing_data["pe"] == 1
        assert "could not be evaluated" in result.note

    def test_sector_filter(self, db, screen_setup):
        companies, quotes, metrics = screen_setup
        result = run_screen(db, companies=companies, quotes=quotes, scores={},
                            metrics=metrics, sectors=["Materials"])
        assert {row.ticker for row in result.rows} == {"CCC"}

    def test_index_filter(self, db, screen_setup):
        companies, quotes, metrics = screen_setup
        result = run_screen(db, companies=companies, quotes=quotes, scores={},
                            metrics=metrics, indices=["EGX30"])
        assert {row.ticker for row in result.rows} == {"AAA", "BBB"}

    def test_demo_quotes_flag_the_whole_run(self, db, screen_setup):
        companies, quotes, metrics = screen_setup
        quotes["AAA"].is_demo = True
        result = run_screen(db, companies=companies, quotes=quotes, scores={},
                            metrics=metrics)
        assert result.is_demo is True


class TestUserAlerts:
    def _alert(self, **kwargs):
        defaults = dict(id=1, user_id=1, ticker="TEST", condition="price_above",
                        threshold=50.0, active=True, email_delivery=True)
        defaults.update(kwargs)
        return saas_models.UserAlert(**defaults)

    def _quote(self, **kwargs):
        defaults = dict(ticker="TEST", price=60.0, is_demo=False, change_pct=0.02)
        defaults.update(kwargs)
        return saas_models.Quote(**defaults)

    def test_price_above_fires(self, db):
        result = evaluate_alert(db, self._alert(), self._quote(price=60.0))
        assert result.triggered
        assert "60" in result.message

    def test_price_above_does_not_fire_below_the_level(self, db):
        assert not evaluate_alert(db, self._alert(), self._quote(price=40.0)).triggered

    def test_never_fires_on_demonstration_data(self, db):
        """The single most damaging thing this system could do."""
        result = evaluate_alert(db, self._alert(), self._quote(price=999.0, is_demo=True))
        assert not result.triggered
        assert "demonstration data" in result.skipped_reason

    def test_missing_quote_is_skipped_not_treated_as_zero(self, db):
        result = evaluate_alert(db, self._alert(condition="price_below"), None)
        assert not result.triggered
        assert "N/A" in result.skipped_reason

    def test_missing_threshold_is_skipped(self, db):
        result = evaluate_alert(db, self._alert(threshold=None), self._quote())
        assert not result.triggered
        assert "threshold" in result.skipped_reason

    def test_paused_alert_never_fires(self, db):
        result = evaluate_alert(db, self._alert(active=False), self._quote(price=999.0))
        assert not result.triggered

    def test_percentage_move_uses_the_daily_change(self, db):
        alert = self._alert(condition="pct_move_up", threshold=0.05)
        assert evaluate_alert(db, alert, self._quote(change_pct=0.06)).triggered
        assert not evaluate_alert(db, alert, self._quote(change_pct=0.04)).triggered

    def test_rsi_alert_needs_price_history(self, db):
        alert = self._alert(condition="rsi_above", threshold=70.0)
        result = evaluate_alert(db, alert, self._quote())
        assert not result.triggered
        assert "history" in result.skipped_reason

    def test_52_week_high_needs_the_stored_high(self, db):
        alert = self._alert(condition="high_52w", threshold=None)
        result = evaluate_alert(db, alert, self._quote(week52_high=None))
        assert not result.triggered
        assert "52-week high" in result.skipped_reason
