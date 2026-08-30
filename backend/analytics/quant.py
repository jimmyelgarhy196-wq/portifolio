"""Quantitative factor engine.

Cross-sectional factor scoring across the EGX universe. Unlike the fundamental
and technical engines, which judge a company on its own merits, this engine
ranks each name *relative to its peers* — which is the only way a factor score
is meaningful.

Six factors: Value, Momentum, Quality, Growth, Liquidity, Volatility. Weights
come from ``config/weights.yaml`` and are configurable at runtime.

Normalisation uses winsorised z-scores. Winsorising at the 5th/95th percentile
matters on the EGX specifically: the universe is small and a single illiquid
name with a distorted ratio would otherwise dominate the cross-section.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from backend.analytics.scoring import ScoreComponent, ScoreResult, build_score
from backend.core.config import load_yaml_config
from backend.core.data_quality import is_available

#: Factor definitions: which raw metric feeds each factor, and its direction.
#: ``higher_is_better=False`` inverts the z-score (cheap value metrics score high).
FACTOR_INPUTS: dict[str, list[tuple[str, bool, float]]] = {
    "value": [
        ("pe", False, 0.30),
        ("pb", False, 0.20),
        ("ev_ebitda", False, 0.25),
        ("fcf_yield", True, 0.25),
    ],
    "momentum": [
        ("momentum_12m", True, 0.35),
        ("momentum_6m", True, 0.30),
        ("momentum_3m", True, 0.25),
        # 1-month is deliberately negative-weighted: short-term reversal is a
        # documented effect, and buying a name that just spiked is not momentum.
        ("momentum_1m", False, 0.10),
    ],
    "quality": [
        ("roe", True, 0.30),
        ("roic", True, 0.30),
        ("net_margin", True, 0.20),
        ("debt_to_equity", False, 0.20),
    ],
    "growth": [
        ("revenue_growth", True, 0.35),
        ("eps_growth", True, 0.35),
        ("revenue_cagr", True, 0.30),
    ],
    "liquidity": [
        ("average_turnover", True, 0.70),
        ("market_cap", True, 0.30),
    ],
    "volatility": [
        # Low-volatility anomaly: lower realised vol scores higher.
        ("volatility_20d", False, 0.60),
        ("atr_pct", False, 0.40),
    ],
}


@dataclass
class FactorExposure:
    """One name's exposure to one factor."""

    factor: str
    score: float | None          # 0-100 cross-sectional
    zscore: float | None
    inputs: dict[str, Any] = field(default_factory=dict)
    coverage: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "score": None if self.score is None else round(self.score, 1),
            "zscore": None if self.zscore is None else round(self.zscore, 3),
            "inputs": self.inputs,
            "coverage": round(self.coverage, 2),
        }


@dataclass
class QuantSnapshot:
    ticker: str
    factors: dict[str, FactorExposure] = field(default_factory=dict)
    score: ScoreResult | None = None
    universe_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "universe_size": self.universe_size,
            "factors": {k: v.to_dict() for k, v in self.factors.items()},
            "score": self.score.to_dict() if self.score else None,
        }


# ---------------------------------------------------------------------------
# Cross-sectional statistics
# ---------------------------------------------------------------------------
def winsorize(values: Sequence[float], lower: float = 0.05, upper: float = 0.95) -> list[float]:
    """Clip values to the [lower, upper] percentile range.

    Essential on a universe this small: one distorted ratio would otherwise
    swamp the z-score for every other name.

    Note on small samples: plain percentile indexing fails to clip anything when
    the tail fraction rounds below one observation — with 10 names and a 95th
    percentile, the cut index lands *on* the outlier. Since the EGX universe is
    small by construction, at least one observation is clipped from each end
    whenever there are five or more names, which is the protection the caller
    actually needs. Below five names, no clipping is applied: with a sample that
    small, clipping would distort more than it protects.
    """
    if not values:
        return []
    ordered = sorted(values)
    n = len(ordered)
    if n < 5:
        return list(values)

    trim_low = max(1, int(math.ceil(n * lower)))
    trim_high = max(1, int(math.ceil(n * (1.0 - upper))))
    # Never trim so far that the retained range collapses.
    trim_low = min(trim_low, (n - 1) // 2)
    trim_high = min(trim_high, (n - 1) // 2)

    lo = ordered[trim_low]
    hi = ordered[n - 1 - trim_high]
    return [max(lo, min(hi, v)) for v in values]


def cross_sectional_zscores(
    values: dict[str, float | None], *, higher_is_better: bool = True
) -> dict[str, float | None]:
    """Z-score each name's value against the universe. Missing stays missing."""
    present = {k: float(v) for k, v in values.items() if is_available(v)}
    if len(present) < 3:
        return {k: None for k in values}

    clipped = dict(zip(present.keys(), winsorize(list(present.values()))))
    mean = sum(clipped.values()) / len(clipped)
    variance = sum((v - mean) ** 2 for v in clipped.values()) / max(1, len(clipped) - 1)
    std = variance**0.5

    out: dict[str, float | None] = {k: None for k in values}
    if std <= 0:
        # Every name identical: no differentiation exists, so no signal.
        for k in present:
            out[k] = 0.0
        return out

    sign = 1.0 if higher_is_better else -1.0
    for key, value in clipped.items():
        out[key] = sign * (value - mean) / std
    return out


def zscore_to_score(z: float | None, *, cap: float = 2.5) -> float | None:
    if z is None:
        return None
    clipped = max(-cap, min(cap, z))
    return 50.0 + (clipped / cap) * 50.0


# ---------------------------------------------------------------------------
# Factor model
# ---------------------------------------------------------------------------
def compute_factor_exposures(
    universe_metrics: dict[str, dict[str, float | None]],
    *,
    factor_inputs: dict[str, list[tuple[str, bool, float]]] | None = None,
) -> dict[str, dict[str, FactorExposure]]:
    """Compute factor exposures for every name in the universe.

    *universe_metrics* maps ``ticker -> {metric_name: value}``. Returns
    ``ticker -> {factor: FactorExposure}``.
    """
    factor_inputs = factor_inputs or FACTOR_INPUTS
    tickers = list(universe_metrics)
    result: dict[str, dict[str, FactorExposure]] = {t: {} for t in tickers}

    for factor, inputs in factor_inputs.items():
        # Z-score each raw metric across the universe first.
        metric_z: dict[str, dict[str, float | None]] = {}
        for metric_name, higher_is_better, _weight in inputs:
            values = {t: universe_metrics[t].get(metric_name) for t in tickers}
            metric_z[metric_name] = cross_sectional_zscores(
                values, higher_is_better=higher_is_better
            )

        # Then blend the z-scores into a single factor exposure per name.
        for ticker in tickers:
            parts: list[tuple[float, float]] = []
            used: dict[str, Any] = {}
            total_weight = sum(w for _, _, w in inputs)
            for metric_name, _hib, weight in inputs:
                z = metric_z[metric_name].get(ticker)
                if z is not None:
                    parts.append((z, weight))
                    raw = universe_metrics[ticker].get(metric_name)
                    used[metric_name] = round(float(raw), 4) if is_available(raw) else None

            if not parts:
                result[ticker][factor] = FactorExposure(factor, None, None, {}, 0.0)
                continue

            weight_used = sum(w for _, w in parts)
            blended = sum(z * w for z, w in parts) / weight_used
            result[ticker][factor] = FactorExposure(
                factor=factor,
                score=zscore_to_score(blended),
                zscore=blended,
                inputs=used,
                coverage=weight_used / total_weight if total_weight else 0.0,
            )

    return result


def score_quant(
    exposures: dict[str, FactorExposure], *, weights: dict[str, float] | None = None
) -> ScoreResult:
    """Combine factor exposures into the 0-100 quantitative score."""
    cfg = weights or load_yaml_config("weights").get("quant") or {}
    components = [
        ScoreComponent(
            factor,
            cfg.get(factor, 100.0 / max(1, len(FACTOR_INPUTS))),
            exposure.score,
            inputs={**exposure.inputs, "zscore": (
                None if exposure.zscore is None else round(exposure.zscore, 3)
            )},
            explanation=(
                f"Cross-sectional {factor} factor: winsorised z-score against the "
                f"EGX universe ({exposure.coverage:.0%} input coverage)."
            ),
        )
        for factor, exposure in exposures.items()
    ]
    return build_score("quantitative", components)


def analyze_universe(
    universe_metrics: dict[str, dict[str, float | None]],
    *,
    weights: dict[str, float] | None = None,
) -> dict[str, QuantSnapshot]:
    """Run the full factor model across the universe."""
    all_exposures = compute_factor_exposures(universe_metrics)
    out: dict[str, QuantSnapshot] = {}
    for ticker, exposures in all_exposures.items():
        out[ticker] = QuantSnapshot(
            ticker=ticker,
            factors=exposures,
            score=score_quant(exposures, weights=weights),
            universe_size=len(universe_metrics),
        )
    return out


def factor_leaders(
    snapshots: dict[str, QuantSnapshot], factor: str, *, top: int = 10
) -> list[tuple[str, float]]:
    """Highest-exposure names for one factor — used by the scanner."""
    ranked = [
        (t, s.factors[factor].score)
        for t, s in snapshots.items()
        if factor in s.factors and s.factors[factor].score is not None
    ]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:top]
