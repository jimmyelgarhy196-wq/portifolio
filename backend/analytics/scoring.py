"""Scoring framework — the explainability backbone.

Every score in GMG is built here, and every score carries its own
derivation: which inputs fed each component, how the component was normalised,
what weight it carried, and how much it contributed to the total.

Two rules make the scores trustworthy:

**Missing data is never scored as average.** A component with no inputs
contributes nothing, and its weight is redistributed across the components that
do have data. Treating "unknown" as 50/100 would invent an opinion.

**Coverage is reported.** When only a fraction of components could be computed,
the result says so and its confidence drops accordingly, so a score built on two
inputs is never mistaken for one built on eight.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from backend.core.config import load_yaml_config
from backend.core.data_quality import Confidence, confidence_from_coverage


@dataclass
class ScoreComponent:
    """One weighted input to a composite score."""

    name: str
    weight: float
    #: 0-100, or ``None`` when the component could not be computed.
    value: float | None
    inputs: dict[str, Any] = field(default_factory=dict)
    explanation: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": round(self.weight, 4),
            "value": None if self.value is None else round(self.value, 2),
            "available": self.available,
            "inputs": self.inputs,
            "explanation": self.explanation,
        }


@dataclass
class ScoreResult:
    """A composite score with a complete audit trail."""

    name: str
    value: float | None
    components: list[ScoreComponent] = field(default_factory=list)
    confidence: Confidence = Confidence.HIGH
    coverage: float = 1.0
    note: str | None = None

    @property
    def available(self) -> bool:
        return self.value is not None

    @property
    def missing_components(self) -> list[str]:
        return [c.name for c in self.components if not c.available]

    def contribution(self, component: ScoreComponent) -> float | None:
        """How many points this component contributed to the final score."""
        if not component.available:
            return None
        effective = self.effective_weight(component)
        return round((component.value or 0.0) * effective, 2)

    def effective_weight(self, component: ScoreComponent) -> float:
        """Weight after redistribution across the available components."""
        total = sum(c.weight for c in self.components if c.available)
        return (component.weight / total) if total > 0 else 0.0

    def breakdown(self) -> list[dict[str, Any]]:
        """Row-per-component derivation, ready for the UI or the API."""
        return [
            {
                **c.to_dict(),
                "effective_weight": round(self.effective_weight(c), 4),
                "contribution": self.contribution(c),
            }
            for c in self.components
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": None if self.value is None else round(self.value, 1),
            "available": self.available,
            "confidence": self.confidence.value,
            "coverage": round(self.coverage, 3),
            "missing": self.missing_components,
            "note": self.note,
            "components": self.breakdown(),
        }

    def explain(self) -> str:
        """Human-readable derivation, used in reports and theses."""
        if not self.available:
            return f"{self.name}: UNAVAILABLE — {self.note or 'no inputs could be computed.'}"
        label = "GMG" if self.name == "egx_alpha" else self.name.replace("_", " ").title()
        lines = [f"{label} Score: {self.value:.0f}/100"]
        for row in self.breakdown():
            if row["available"]:
                lines.append(
                    f"  {row['name'].replace('_', ' ').title():<24} "
                    f"{row['value']:>6.1f}  × {row['effective_weight']:.0%} "
                    f"= {row['contribution']:.1f}"
                )
            else:
                lines.append(
                    f"  {row['name'].replace('_', ' ').title():<24} "
                    f"{'UNAVAILABLE':>6}  (weight redistributed)"
                )
        if self.coverage < 1.0:
            lines.append(
                f"  Coverage: {self.coverage:.0%} of components — "
                f"confidence {self.confidence.value}."
            )
        return "\n".join(lines)


def build_score(
    name: str,
    components: Sequence[ScoreComponent],
    *,
    min_coverage: float = 0.34,
) -> ScoreResult:
    """Combine weighted components into a score with full provenance.

    Weight from unavailable components is redistributed across the available
    ones. If coverage falls below *min_coverage* the score is withheld entirely
    rather than published on too thin a base.
    """
    components = list(components)
    if not components:
        return ScoreResult(
            name=name, value=None, components=[],
            confidence=Confidence.UNVERIFIED, coverage=0.0,
            note="No components were supplied.",
        )

    total_weight = sum(c.weight for c in components)
    available = [c for c in components if c.available]
    available_weight = sum(c.weight for c in available)
    coverage = (available_weight / total_weight) if total_weight > 0 else 0.0

    if not available or coverage < min_coverage:
        missing = ", ".join(c.name for c in components if not c.available)
        return ScoreResult(
            name=name, value=None, components=components,
            confidence=Confidence.UNVERIFIED, coverage=coverage,
            note=(
                f"Insufficient data: only {coverage:.0%} of component weight could be "
                f"computed (minimum {min_coverage:.0%}). Missing: {missing}."
            ),
        )

    value = sum((c.value or 0.0) * (c.weight / available_weight) for c in available)
    confidence = confidence_from_coverage(len(available), len(components))

    note = None
    if coverage < 1.0:
        note = (
            f"{len(components) - len(available)} of {len(components)} components "
            f"lacked data; their weight was redistributed."
        )

    return ScoreResult(
        name=name,
        value=max(0.0, min(100.0, value)),
        components=components,
        confidence=confidence,
        coverage=coverage,
        note=note,
    )


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
def normalize_linear(
    value: float | None, worst: float, best: float, *, clamp: bool = True
) -> float | None:
    """Map *value* onto 0-100 between *worst* and *best*.

    Supports inverted scales (``worst > best``), which is how "lower P/E is
    better" is expressed without a special case.
    """
    if value is None:
        return None
    if worst == best:
        return 50.0
    score = (value - worst) / (best - worst) * 100.0
    return max(0.0, min(100.0, score)) if clamp else score


def normalize_bands(value: float | None, bands: Sequence[tuple[float, float]]) -> float | None:
    """Map *value* through ``[(threshold, score), ...]`` sorted ascending.

    The first band whose threshold the value does not exceed wins. Useful where
    the relationship is genuinely stepped rather than linear.
    """
    if value is None:
        return None
    for threshold, score in bands:
        if value <= threshold:
            return score
    return bands[-1][1] if bands else None


def percentile_rank(value: float | None, population: Iterable[float | None]) -> float | None:
    """Percentile of *value* within *population*, as 0-100.

    This is how cross-sectional (relative-to-universe) scoring is done.
    """
    if value is None:
        return None
    peers = [p for p in population if p is not None]
    if len(peers) < 2:
        return None
    below = sum(1 for p in peers if p < value)
    equal = sum(1 for p in peers if p == value)
    return ((below + 0.5 * equal) / len(peers)) * 100.0


def zscore(value: float | None, population: Iterable[float | None]) -> float | None:
    """Standard score of *value* within *population*."""
    if value is None:
        return None
    peers = [p for p in population if p is not None]
    if len(peers) < 3:
        return None
    mean = sum(peers) / len(peers)
    variance = sum((p - mean) ** 2 for p in peers) / (len(peers) - 1)
    if variance <= 0:
        return 0.0
    return (value - mean) / (variance**0.5)


def zscore_to_score(z: float | None, *, cap: float = 2.5) -> float | None:
    """Map a z-score onto 0-100, clipping at ±*cap* standard deviations."""
    if z is None:
        return None
    clipped = max(-cap, min(cap, z))
    return 50.0 + (clipped / cap) * 50.0


def load_weights(block: str) -> dict[str, float]:
    """Read a weight block from ``config/weights.yaml``."""
    return dict(load_yaml_config("weights").get(block) or {})
