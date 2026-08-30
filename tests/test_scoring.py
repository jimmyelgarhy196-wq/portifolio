"""Scoring framework and the fundamental/technical/quant engines."""
from __future__ import annotations

from datetime import date

import pytest

from backend.analytics.fundamental import (
    FinancialPeriod,
    analyze_fundamentals,
    compute_metrics,
    detect_scale_mismatch,
)
from backend.analytics.quant import (
    analyze_universe,
    cross_sectional_zscores,
    winsorize,
)
from backend.analytics.scoring import (
    ScoreComponent,
    build_score,
    normalize_bands,
    normalize_linear,
    percentile_rank,
    zscore,
)
from backend.core.data_quality import UNAVAILABLE, Confidence


class TestBuildScore:
    def test_full_coverage_weighted_mean(self):
        result = build_score("t", [
            ScoreComponent("a", 50, 80.0),
            ScoreComponent("b", 50, 60.0),
        ])
        assert result.value == pytest.approx(70.0)
        assert result.coverage == 1.0
        assert result.confidence is Confidence.HIGH

    def test_missing_component_redistributes_weight_not_scored_as_average(self):
        # If the missing component were scored 50, the result would be 65.
        # Redistribution gives 80 — the score reflects only what is known.
        result = build_score("t", [
            ScoreComponent("a", 50, 80.0),
            ScoreComponent("b", 50, None),
        ])
        assert result.value == pytest.approx(80.0)
        assert result.missing_components == ["b"]
        assert "redistributed" in result.note

    def test_below_min_coverage_withholds_score(self):
        components = [ScoreComponent("a", 25, 90.0)] + [
            ScoreComponent(name, 25, None) for name in ("b", "c", "d")
        ]
        result = build_score("t", components)
        assert result.value is None
        assert not result.available
        assert "Insufficient data" in result.note

    def test_no_components(self):
        result = build_score("t", [])
        assert result.value is None
        assert result.confidence is Confidence.UNVERIFIED

    def test_breakdown_contributions_sum_to_value(self):
        result = build_score("t", [
            ScoreComponent("a", 30, 90.0),
            ScoreComponent("b", 20, 60.0),
            ScoreComponent("c", 50, 40.0),
        ])
        total = sum(r["contribution"] for r in result.breakdown() if r["available"])
        assert total == pytest.approx(result.value, abs=0.05)

    def test_value_clamped_to_100(self):
        result = build_score("t", [ScoreComponent("a", 100, 150.0)])
        assert result.value == 100.0

    def test_explain_shows_derivation(self):
        result = build_score("t", [
            ScoreComponent("valuation", 60, 90.0),
            ScoreComponent("growth", 40, None),
        ])
        text = result.explain()
        assert "Valuation" in text and "UNAVAILABLE" in text and "redistributed" in text


class TestNormalisation:
    def test_linear(self):
        assert normalize_linear(5, 0, 10) == 50.0
        assert normalize_linear(0, 0, 10) == 0.0
        assert normalize_linear(10, 0, 10) == 100.0

    def test_inverted_scale_for_lower_is_better(self):
        # P/E of 8 where 30 is worst and 5 is best.
        assert normalize_linear(8, 30, 5) == pytest.approx(88.0)

    def test_clamped(self):
        assert normalize_linear(-5, 0, 10) == 0.0
        assert normalize_linear(50, 0, 10) == 100.0

    def test_none_passes_through(self):
        assert normalize_linear(None, 0, 10) is None

    def test_bands(self):
        bands = [(0.3, 100), (0.6, 70), (1.0, 40), (2.0, 10)]
        assert normalize_bands(0.2, bands) == 100
        assert normalize_bands(0.4, bands) == 70
        assert normalize_bands(5.0, bands) == 10

    def test_percentile_rank(self):
        assert percentile_rank(15, [10, 12, 15, 20, 30]) == 50.0
        assert percentile_rank(5, [10, 12, 15]) == 0.0

    def test_percentile_needs_population(self):
        assert percentile_rank(5, [10]) is None

    def test_zscore_needs_three_points(self):
        assert zscore(5, [1, 2]) is None
        assert zscore(10, [10, 10, 10]) == 0.0


class TestFundamentalMetrics:
    @staticmethod
    def _periods(**overrides):
        base = dict(
            ticker="T", period="2024-FY", period_type="FY",
            period_end=date(2024, 12, 31), available_from=date(2025, 3, 31),
            revenue=1000.0, gross_profit=400.0, ebitda=280.0, operating_income=240.0,
            net_income=150.0, eps=1.5, cash=100.0, total_debt=300.0,
            total_assets=1800.0, total_equity=900.0, operating_cash_flow=180.0,
            capex=50.0, free_cash_flow=130.0, interest_expense=20.0,
            current_assets=500.0, current_liabilities=250.0, dividends_paid=45.0,
        )
        base.update(overrides)
        prior = dict(base)
        prior.update({
            "period": "2023-FY", "period_end": date(2023, 12, 31),
            "revenue": 900.0, "net_income": 120.0, "eps": 1.2,
            "total_equity": 800.0, "ebitda": 250.0,
        })
        return [FinancialPeriod(**base), FinancialPeriod(**prior)]

    def test_growth_and_margins(self):
        metrics = compute_metrics(self._periods())
        assert metrics["revenue_growth"].value == pytest.approx(0.1111, rel=1e-3)
        assert metrics["net_margin"].value == pytest.approx(0.15)
        assert metrics["gross_margin"].value == pytest.approx(0.40)

    def test_roe_uses_average_equity(self):
        metrics = compute_metrics(self._periods())
        # 150 / ((900+800)/2) = 0.1765, not 150/900 = 0.1667
        assert metrics["roe"].value == pytest.approx(150 / 850, rel=1e-4)

    def test_pe_not_computed_on_losses(self):
        metrics = compute_metrics(self._periods(net_income=-100.0, eps=-1.0), price=20.0)
        assert not metrics["pe"].available

    def test_division_by_zero_yields_unavailable(self):
        metrics = compute_metrics(self._periods(revenue=0.0))
        assert not metrics["net_margin"].available

    def test_missing_inputs_stay_unavailable(self):
        metrics = compute_metrics(self._periods(ebitda=None))
        assert not metrics["ebitda_margin"].available
        assert not metrics["net_debt_to_ebitda"].available

    def test_fcf_computed_when_absent(self):
        metrics = compute_metrics(
            self._periods(free_cash_flow=None, operating_cash_flow=180.0, capex=50.0)
        )
        assert metrics["free_cash_flow"].value == pytest.approx(130.0)

    def test_valuation_requires_share_count(self):
        metrics = compute_metrics(self._periods(), price=20.0)
        assert not metrics["pb"].available   # no share count -> no market cap
        metrics = compute_metrics(self._periods(), price=20.0, shares_outstanding=100.0)
        assert metrics["pb"].available


class TestUnitScaleGuard:
    def test_detects_millions_mismatch(self):
        # market cap in absolute EGP, revenue in millions
        assert detect_scale_mismatch(16_000_000_000, 12_000, 9_000) == 1e6

    def test_no_mismatch_when_consistent(self):
        assert detect_scale_mismatch(16e9, 12e9, 9e9) is None

    def test_implausible_multiple_is_withheld(self):
        period = FinancialPeriod(
            ticker="T", period="2024-FY", period_type="FY",
            period_end=date(2024, 12, 31),
            revenue=12_000_000_000, net_income=1_800_000_000,
            total_equity=0.5,   # corrupt
            total_debt=3_000_000_000, cash=1_500_000_000,
            ebitda=2_900_000_000, operating_income=2_400_000_000, eps=3.6,
        )
        metrics = compute_metrics([period], price=32.0, shares_outstanding=500e6)
        assert not metrics["pb"].available
        assert "units mismatch" in metrics["pb"].note
        assert metrics["ps"].available   # unaffected metrics survive

    def test_explicit_scale_matches_detection(self):
        periods = [FinancialPeriod(
            ticker="T", period="2024-FY", period_type="FY",
            period_end=date(2024, 12, 31), revenue=12_000, net_income=1_800,
            total_equity=9_000, total_debt=3_000, cash=1_500, ebitda=2_900,
            operating_income=2_400, eps=3.6,
        )]
        detected = compute_metrics(periods, price=32.0, shares_outstanding=500e6)
        explicit = compute_metrics(
            periods, price=32.0, shares_outstanding=500e6, statement_scale=1e6
        )
        assert detected["pb"].value == pytest.approx(explicit["pb"].value)


class TestFundamentalScore:
    def test_no_statements_reports_insufficient(self):
        snapshot = analyze_fundamentals("T", [])
        assert snapshot.insufficient_data
        assert snapshot.score is None
        assert "No financial statements" in snapshot.note

    def test_single_period_notes_missing_growth(self):
        period = FinancialPeriod(
            ticker="T", period="2024-FY", period_type="FY",
            period_end=date(2024, 12, 31), revenue=1000.0, net_income=150.0,
            total_equity=900.0, ebitda=280.0, operating_income=240.0, eps=1.5,
            gross_profit=400.0, total_debt=300.0, cash=100.0,
            operating_cash_flow=180.0, capex=50.0,
        )
        snapshot = analyze_fundamentals("T", [period], price=20.0, shares_outstanding=100.0)
        assert "growth metrics could not be computed" in snapshot.note


class TestQuantFactors:
    def test_winsorize_clips_outliers(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 1000]
        assert max(winsorize(values)) < 1000

    def test_outlier_cannot_dominate_cross_section(self):
        values = {f"S{i}": float(i + 10) for i in range(20)}
        values["OUT"] = 99999.0
        scores = cross_sectional_zscores(values)
        assert abs(scores["OUT"]) < 10

    def test_lower_is_better_inverts(self):
        values = {"cheap": 5.0, "mid": 15.0, "rich": 30.0, "x": 20.0}
        higher = cross_sectional_zscores(values, higher_is_better=True)
        lower = cross_sectional_zscores(values, higher_is_better=False)
        assert higher["cheap"] < 0 and lower["cheap"] > 0

    def test_missing_stays_missing(self):
        values = {"a": 1.0, "b": 2.0, "c": 3.0, "d": None}
        assert cross_sectional_zscores(values)["d"] is None

    def test_too_few_names_yields_nothing(self):
        assert cross_sectional_zscores({"a": 1.0, "b": 2.0}) == {"a": None, "b": None}

    def test_identical_universe_has_no_signal(self):
        values = {f"X{i}": 10.0 for i in range(5)}
        assert all(v == 0.0 for v in cross_sectional_zscores(values).values())

    def test_sparse_name_gets_partial_coverage(self):
        universe = {
            f"S{i}": {
                "pe": 10.0 + i, "roe": 0.1 + i / 100, "momentum_12m": i / 50,
                "revenue_growth": i / 60, "average_turnover": 1e6 * (i + 1),
                "volatility_20d": 0.2 + i / 100,
            }
            for i in range(10)
        }
        universe["SPARSE"] = {"pe": 8.0}
        snapshots = analyze_universe(universe)
        sparse = snapshots["SPARSE"]
        assert sparse.factors["value"].coverage < 1.0
        assert sparse.factors["quality"].score is None
