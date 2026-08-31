"""Rating, confidence, horizon and category, derived from the scores.

These four labels are the summary a subscriber reads first, so they are
computed by explicit rules rather than written by a language model. That keeps
them reproducible, auditable, and impossible to hallucinate: the same inputs
always produce the same rating, and every rating carries the reason it was
reached.

A rating is withheld — not defaulted to HOLD — when the underlying score is
unavailable or rests on too little data. "We do not have enough information"
is a real answer and the product gives it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.analytics.scoring import Confidence

#: Rating bands over the 0-100 master score.
RATING_BANDS: list[tuple[float, str, str]] = [
    (80.0, "STRONG_BUY", "Strong Buy"),
    (65.0, "BUY", "Buy"),
    (45.0, "HOLD", "Hold"),
    (32.0, "REDUCE", "Reduce"),
    (0.0, "SELL", "Sell"),
]

#: Below this coverage the score is not a sound basis for a rating.
MIN_COVERAGE_FOR_RATING = 0.45

HORIZONS = {
    "SHORT": ("Short term", "0–3 months"),
    "MEDIUM": ("Medium term", "3–12 months"),
    "LONG": ("Long term", "1–3 years"),
}

CATEGORIES = {
    "FUNDAMENTAL": (
        "Fundamental",
        "The case rests on the company's financial results and valuation.",
    ),
    "TECHNICAL": (
        "Technical",
        "The case rests on price behaviour, trend and momentum rather than the accounts.",
    ),
    "HYBRID": (
        "Hybrid",
        "Fundamentals and price action point the same way; both support the case.",
    ),
    "UNCLASSIFIED": (
        "Unclassified",
        "Neither the fundamental nor the technical picture is complete enough to classify.",
    ),
}


@dataclass
class ResearchRating:
    available: bool
    code: str | None = None
    label: str = "Not rated"
    score: float | None = None
    confidence: str = "LOW"
    confidence_label: str = "Low"
    horizon: str | None = None
    horizon_label: str = "N/A"
    horizon_window: str = ""
    category: str = "UNCLASSIFIED"
    category_label: str = "Unclassified"
    category_note: str = ""
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _band(score: float) -> tuple[str, str]:
    for threshold, code, label in RATING_BANDS:
        if score >= threshold:
            return code, label
    return "SELL", "Sell"


def _sub_value(sub) -> float | None:
    return sub.value if (sub is not None and sub.available) else None


def derive_rating(alpha) -> ResearchRating:
    """Turn an :class:`AlphaScore` into the four published labels.

    *alpha* is the master score object from :mod:`backend.analytics.master_score`.
    """
    score = alpha.value
    coverage = alpha.score.coverage if alpha.score else 0.0

    if score is None:
        return ResearchRating(
            available=False,
            unavailable_reason=(
                "N/A — data unavailable. Too little of the underlying data is present to "
                "produce a rating, and GMG does not issue a default rating in place of one."
            ),
        )
    if coverage < MIN_COVERAGE_FOR_RATING:
        return ResearchRating(
            available=False, score=score,
            unavailable_reason=(
                f"Rating withheld. The score rests on {coverage:.0%} of the intended inputs, "
                f"below the {MIN_COVERAGE_FOR_RATING:.0%} GMG requires before publishing a "
                "rating."
            ),
        )

    code, label = _band(score)

    fundamental = _sub_value(alpha.fundamental)
    technical = _sub_value(alpha.technical)
    quant = _sub_value(alpha.quant)
    risk = _sub_value(alpha.risk)

    # --- Confidence -------------------------------------------------------
    # Starts from the score's own confidence, then is reduced by thin data or
    # stale inputs. It is never raised above what the score itself supports.
    base = alpha.score.confidence if alpha.score else Confidence.LOW
    order = ["LOW", "MEDIUM", "HIGH"]
    level = order.index(base.value) if base.value in order else 0
    caveats: list[str] = []

    if fundamental is None:
        level = min(level, 1)
        caveats.append("No fundamental score is available, so the rating leans on price data.")
    if technical is None:
        caveats.append("No technical score is available for this instrument.")
    if alpha.staleness:
        level = max(0, level - 1)
        caveats.append(
            f"{len(alpha.staleness)} input(s) are older than GMG's freshness threshold."
        )
    if coverage < 0.7:
        level = min(level, 1)
        caveats.append(f"Score coverage is {coverage:.0%} of the intended inputs.")
    for warning in alpha.warnings:
        caveats.append(warning)

    confidence = order[level]
    confidence_label = confidence.title()

    # --- Horizon ----------------------------------------------------------
    # Whichever engine is actually driving the score sets the horizon: a case
    # built on the accounts plays out over quarters, one built on a moving
    # average over weeks.
    if fundamental is not None and technical is not None:
        gap = fundamental - technical
        if gap >= 12:
            horizon = "LONG"
        elif gap <= -12:
            horizon = "SHORT"
        else:
            horizon = "MEDIUM"
    elif fundamental is not None:
        horizon = "LONG"
    elif technical is not None:
        horizon = "SHORT"
    else:
        horizon = "MEDIUM"
    horizon_label, horizon_window = HORIZONS[horizon]

    # --- Category ---------------------------------------------------------
    strong = 60.0
    if fundamental is not None and technical is not None:
        if fundamental >= strong and technical >= strong:
            category = "HYBRID"
        elif fundamental >= technical:
            category = "FUNDAMENTAL"
        else:
            category = "TECHNICAL"
    elif fundamental is not None:
        category = "FUNDAMENTAL"
    elif technical is not None:
        category = "TECHNICAL"
    else:
        category = "UNCLASSIFIED"
    category_label, category_note = CATEGORIES[category]

    # --- Reasons ----------------------------------------------------------
    reasons: list[str] = [
        f"Master score {score:.0f}/100 falls in the {label} band."
    ]
    if fundamental is not None:
        reasons.append(f"Fundamental score {fundamental:.0f}/100.")
    if technical is not None:
        reasons.append(f"Technical score {technical:.0f}/100.")
    if quant is not None:
        reasons.append(f"Quantitative factor score {quant:.0f}/100.")
    if risk is not None:
        reasons.append(f"Risk score {risk:.0f}/100 (higher is safer).")

    return ResearchRating(
        available=True, code=code, label=label, score=score,
        confidence=confidence, confidence_label=confidence_label,
        horizon=horizon, horizon_label=horizon_label, horizon_window=horizon_window,
        category=category, category_label=category_label, category_note=category_note,
        reasons=reasons, caveats=caveats,
    )
