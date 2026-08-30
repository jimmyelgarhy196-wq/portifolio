"""Data-quality primitives: the no-fabrication guarantees."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.core.data_quality import (
    UNAVAILABLE,
    Claim,
    Confidence,
    DataQuality,
    Sourced,
    assess_staleness,
    confidence_from_coverage,
    is_available,
    safe_div,
    safe_growth,
)


class TestUnavailable:
    def test_is_falsy(self):
        assert not UNAVAILABLE
        assert not is_available(UNAVAILABLE)

    def test_is_singleton(self):
        from backend.core.data_quality import _Unavailable

        assert _Unavailable() is UNAVAILABLE

    def test_arithmetic_propagates(self):
        assert UNAVAILABLE + 5 is UNAVAILABLE
        assert 5 + UNAVAILABLE is UNAVAILABLE
        assert UNAVAILABLE * 3 is UNAVAILABLE
        assert UNAVAILABLE / 2 is UNAVAILABLE
        assert 10 / UNAVAILABLE is UNAVAILABLE
        assert -UNAVAILABLE is UNAVAILABLE

    def test_comparison_never_true(self):
        # "Unknown" is neither greater nor less than anything. A filter like
        # `if pe < 15` must never admit a stock whose P/E is unknown.
        assert not (UNAVAILABLE > 100)
        assert not (UNAVAILABLE < 100)
        assert not (UNAVAILABLE >= 0)
        assert not (UNAVAILABLE <= 0)

    def test_equality_only_with_itself(self):
        assert UNAVAILABLE == UNAVAILABLE
        assert UNAVAILABLE != 0
        assert UNAVAILABLE != None  # noqa: E711


class TestSafeDiv:
    def test_normal(self):
        assert safe_div(10, 4) == 2.5

    def test_zero_denominator_is_unavailable_not_infinity(self):
        # Returning inf or 0 here would both be fabrications of a real ratio.
        assert safe_div(10, 0) is UNAVAILABLE

    def test_none_inputs(self):
        assert safe_div(None, 5) is UNAVAILABLE
        assert safe_div(5, None) is UNAVAILABLE
        assert safe_div(UNAVAILABLE, 5) is UNAVAILABLE

    def test_negative_denominator_rejected_when_disallowed(self):
        # A P/E on negative earnings is not a meaningful multiple.
        assert safe_div(100, -5, allow_negative_denom=False) is UNAVAILABLE
        assert safe_div(100, -5, allow_negative_denom=True) == -20.0

    def test_nan_input(self):
        assert safe_div(float("nan"), 5) is UNAVAILABLE


class TestSafeGrowth:
    def test_normal(self):
        assert safe_growth(114, 100) == pytest.approx(0.14)

    def test_negative_base_is_unavailable(self):
        # "Profit grew 250%" from a loss is economically meaningless.
        assert safe_growth(100, -50) is UNAVAILABLE

    def test_zero_base_is_unavailable(self):
        assert safe_growth(100, 0) is UNAVAILABLE

    def test_decline(self):
        assert safe_growth(80, 100) == pytest.approx(-0.20)


class TestStaleness:
    def test_fresh_data_no_finding(self):
        recent = datetime.now(timezone.utc) - timedelta(days=2)
        assert assess_staleness("price", recent) is None

    def test_stale_fundamentals_flagged(self):
        old = datetime.now(timezone.utc) - timedelta(days=430)
        finding = assess_staleness("fundamentals", old)
        assert finding is not None
        assert finding.severity == "critical"
        assert "14 months old" in finding.message
        assert "confidence reduced" in finding.message.lower()

    def test_warning_before_critical(self):
        moderate = datetime.now(timezone.utc) - timedelta(days=260)
        finding = assess_staleness("fundamentals", moderate)
        assert finding is not None and finding.severity == "warning"

    def test_unknown_dataset(self):
        assert assess_staleness("nonsense", datetime.now(timezone.utc)) is None


class TestConfidence:
    def test_coverage_mapping(self):
        assert confidence_from_coverage(10, 10) is Confidence.HIGH
        assert confidence_from_coverage(8, 10) is Confidence.MEDIUM
        assert confidence_from_coverage(5, 10) is Confidence.LOW
        assert confidence_from_coverage(1, 10) is Confidence.UNVERIFIED
        assert confidence_from_coverage(0, 0) is Confidence.UNVERIFIED

    def test_factors_are_ordered(self):
        assert (
            Confidence.HIGH.factor
            > Confidence.MEDIUM.factor
            > Confidence.LOW.factor
            > Confidence.UNVERIFIED.factor
        )


class TestSourced:
    def test_carries_provenance(self):
        quality = DataQuality(source="csv:test", retrieved_at=datetime.now(timezone.utc))
        value = Sourced(value=12.5, quality=quality)
        assert value.available
        assert value.to_dict()["source"] == "csv:test"

    def test_synthetic_is_detectable(self):
        quality = DataQuality(
            source="SYNTHETIC_DEMO:synthetic", retrieved_at=datetime.now(timezone.utc)
        )
        assert quality.is_synthetic
        assert quality.to_dict()["synthetic"] is True

    def test_unavailable_value_reports_unavailable(self):
        quality = DataQuality(source="x", retrieved_at=datetime.now(timezone.utc))
        assert not Sourced(value=UNAVAILABLE, quality=quality).available


def test_claim_tags_complete():
    assert {c.value for c in Claim} == {
        "FACT", "CALCULATION", "INFERENCE", "OPINION", "UNKNOWN"
    }
