"""Data-quality primitives — the backbone of the no-fabrication guarantee.

Two ideas carry the whole system:

``Sourced``
    A value that cannot exist without a provenance record. Any number that
    reaches a score, a report, or the UI is wrapped in one of these, so
    "where did this come from?" is always answerable.

``UNAVAILABLE``
    A first-class, singleton absence marker. Arithmetic on it propagates
    rather than raising or silently defaulting to zero, which is what stops a
    missing input from quietly becoming a fabricated one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Confidence(str, Enum):
    """How much weight a consumer should place on a value."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNVERIFIED = "UNVERIFIED"

    @property
    def factor(self) -> float:
        return {
            Confidence.HIGH: 1.0,
            Confidence.MEDIUM: 0.85,
            Confidence.LOW: 0.6,
            Confidence.UNVERIFIED: 0.35,
        }[self]


class Claim(str, Enum):
    """Epistemic status of a statement. Enforced on all AI output."""

    FACT = "FACT"
    CALCULATION = "CALCULATION"
    INFERENCE = "INFERENCE"
    OPINION = "OPINION"
    UNKNOWN = "UNKNOWN"


class _Unavailable:
    """Singleton marker for data that is genuinely not available.

    Propagates through arithmetic and comparison so a missing input can never
    be mistaken for a real value. Falsy, so ``if value:`` guards work.
    """

    _instance: "_Unavailable | None" = None

    def __new__(cls) -> "_Unavailable":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNAVAILABLE"

    def __str__(self) -> str:
        return "—"

    def __bool__(self) -> bool:
        return False

    # Arithmetic propagates rather than raising or defaulting.
    def _propagate(self, *_: Any) -> "_Unavailable":
        return self

    __add__ = __radd__ = __sub__ = __rsub__ = _propagate
    __mul__ = __rmul__ = __truediv__ = __rtruediv__ = _propagate
    __floordiv__ = __rfloordiv__ = __pow__ = __rpow__ = __neg__ = _propagate

    # Ordering comparisons are always False: unknown is never greater/less.
    def __lt__(self, _: Any) -> bool:
        return False

    __le__ = __gt__ = __ge__ = __lt__

    def __eq__(self, other: Any) -> bool:
        return other is self

    def __hash__(self) -> int:
        return hash("__EGX_UNAVAILABLE__")


UNAVAILABLE = _Unavailable()


def is_available(value: Any) -> bool:
    """True when *value* is a usable number (not UNAVAILABLE, None, or NaN)."""
    if value is UNAVAILABLE or value is None:
        return False
    if isinstance(value, float) and value != value:  # NaN
        return False
    return True


def safe_div(numerator: Any, denominator: Any, *, allow_negative_denom: bool = True) -> Any:
    """Division that returns UNAVAILABLE instead of raising or fabricating.

    A zero denominator yields UNAVAILABLE — never infinity and never zero,
    both of which would be silent fabrications of a real ratio.
    """
    if not is_available(numerator) or not is_available(denominator):
        return UNAVAILABLE
    try:
        d = float(denominator)
        if d == 0.0:
            return UNAVAILABLE
        if not allow_negative_denom and d < 0:
            return UNAVAILABLE
        return float(numerator) / d
    except (TypeError, ValueError, ZeroDivisionError):
        return UNAVAILABLE


def safe_growth(current: Any, prior: Any) -> Any:
    """Growth rate that refuses to lie about sign flips.

    Growth from a negative or zero base is mathematically defined but
    economically meaningless (e.g. "profit grew 250%" from a loss), so it
    returns UNAVAILABLE rather than a misleading percentage.
    """
    if not is_available(current) or not is_available(prior):
        return UNAVAILABLE
    p = float(prior)
    if p <= 0:
        return UNAVAILABLE
    return (float(current) - p) / p


@dataclass(frozen=True)
class DataQuality:
    """Provenance attached to every ingested or derived datum."""

    source: str
    retrieved_at: datetime
    data_period: str | None = None
    confidence: Confidence = Confidence.HIGH
    note: str | None = None

    @property
    def is_synthetic(self) -> bool:
        return self.source.upper().startswith("SYNTHETIC")

    def age_days(self, as_of: datetime | date | None = None) -> float:
        ref = as_of or datetime.now(timezone.utc)
        if isinstance(ref, date) and not isinstance(ref, datetime):
            ref = datetime(ref.year, ref.month, ref.day, tzinfo=timezone.utc)
        retrieved = self.retrieved_at
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return (ref - retrieved).total_seconds() / 86400.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "retrieved_at": self.retrieved_at.isoformat(),
            "data_period": self.data_period,
            "confidence": self.confidence.value,
            "note": self.note,
            "synthetic": self.is_synthetic,
        }


@dataclass(frozen=True)
class Sourced(Generic[T]):
    """A value bound to its provenance. Cannot be constructed without a source."""

    value: T
    quality: DataQuality

    @property
    def available(self) -> bool:
        return is_available(self.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": None if not self.available else self.value,
            "available": self.available,
            **self.quality.to_dict(),
        }


# ---------------------------------------------------------------------------
# Staleness policy
# ---------------------------------------------------------------------------
#: Beyond these ages a dataset is considered stale and confidence is reduced.
STALENESS_THRESHOLDS_DAYS: dict[str, float] = {
    "price": 5,
    "fundamentals": 200,      # ~2 quarters; EGX filers are often slow
    "valuation": 5,
    "news": 30,
    "disclosure": 45,
}


@dataclass
class StalenessFinding:
    dataset: str
    age_days: float
    threshold_days: float
    message: str
    severity: str  # "warning" | "critical"

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "age_days": round(self.age_days, 1),
            "threshold_days": self.threshold_days,
            "message": self.message,
            "severity": self.severity,
        }


def assess_staleness(
    dataset: str, retrieved_at: datetime | None, as_of: datetime | None = None
) -> StalenessFinding | None:
    """Return a finding when *dataset* is older than its threshold."""
    threshold = STALENESS_THRESHOLDS_DAYS.get(dataset)
    if threshold is None or retrieved_at is None:
        return None
    quality = DataQuality(source="n/a", retrieved_at=retrieved_at)
    age = quality.age_days(as_of)
    if age <= threshold:
        return None
    months = age / 30.44
    severity = "critical" if age > threshold * 2 else "warning"
    return StalenessFinding(
        dataset=dataset,
        age_days=age,
        threshold_days=threshold,
        severity=severity,
        message=(
            f"{dataset.capitalize()} data is {months:.0f} months old "
            f"({age:.0f} days). Recommendation confidence reduced."
        ),
    )


def confidence_from_coverage(available: int, total: int) -> Confidence:
    """Derive confidence from how many required inputs were actually present."""
    if total <= 0:
        return Confidence.UNVERIFIED
    ratio = available / total
    if ratio >= 0.9:
        return Confidence.HIGH
    if ratio >= 0.7:
        return Confidence.MEDIUM
    if ratio >= 0.4:
        return Confidence.LOW
    return Confidence.UNVERIFIED


@dataclass
class Statement:
    """A single tagged claim produced by an AI agent or a deterministic engine."""

    claim: Claim
    text: str
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"claim": self.claim.value, "text": self.text, "sources": self.sources}

    def render(self) -> str:
        return f"{self.claim.value}: {self.text}"
