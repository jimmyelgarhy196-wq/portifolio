"""Position sizing.

Equal-weight allocation is the default this system deliberately rejects. Size
here is a function of conviction, volatility, risk to the invalidation level,
correlation with what is already held, and remaining sector headroom — and the
engine always explains, in words, why it arrived at the number it did.

Sizing runs as a pipeline of constraints. Each stage can only reduce the
position, and each records its reasoning, so the final recommendation reads as
an argument rather than an assertion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from backend.core.config import load_yaml_config
from backend.core.data_quality import is_available, safe_div


@dataclass
class SizingInputs:
    ticker: str
    direction: str = "LONG"
    strategy: str = "fundamental_long"
    sector: str | None = None
    conviction: float | None = None            # 0-10
    entry_price: float | None = None
    target_price: float | None = None
    invalidation_price: float | None = None
    annual_volatility: float | None = None
    average_turnover: float | None = None      # EGP/day
    correlated_holdings: int = 0
    current_sector_weight: float = 0.0
    current_cash_weight: float = 1.0
    current_speculative_weight: float = 0.0
    portfolio_value: float | None = None


@dataclass
class SizingStep:
    """One stage of the sizing pipeline, with its effect and rationale."""

    name: str
    weight_before: float
    weight_after: float
    reason: str

    @property
    def changed(self) -> bool:
        return abs(self.weight_after - self.weight_before) > 1e-9

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight_before": round(self.weight_before, 4),
            "weight_after": round(self.weight_after, 4),
            "changed": self.changed,
            "reason": self.reason,
        }


@dataclass
class SizingResult:
    ticker: str
    recommended_weight: float
    recommended_value: float | None = None
    recommended_quantity: int | None = None
    steps: list[SizingStep] = field(default_factory=list)
    rejected: bool = False
    rejection_reason: str | None = None
    risk_per_position: float | None = None
    risk_reward: float | None = None

    @property
    def binding_constraint(self) -> str | None:
        """Which stage actually determined the final size."""
        reducing = [s for s in self.steps if s.weight_after < s.weight_before - 1e-9]
        return reducing[-1].name if reducing else None

    def explain(self) -> str:
        if self.rejected:
            return f"No position recommended: {self.rejection_reason}"
        lines = [f"Recommended allocation: {self.recommended_weight:.1%}", "", "Reason:"]
        for step in self.steps:
            if step.changed:
                lines.append(
                    f"  {step.weight_before:.1%} → {step.weight_after:.1%}  {step.reason}"
                )
            else:
                lines.append(f"  {'':>13}  {step.reason}")
        if self.binding_constraint:
            lines.append("")
            lines.append(f"Binding constraint: {self.binding_constraint}.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "recommended_weight": round(self.recommended_weight, 4),
            "recommended_value": self.recommended_value,
            "recommended_quantity": self.recommended_quantity,
            "rejected": self.rejected,
            "rejection_reason": self.rejection_reason,
            "risk_per_position": self.risk_per_position,
            "risk_reward": self.risk_reward,
            "binding_constraint": self.binding_constraint,
            "steps": [s.to_dict() for s in self.steps],
            "explanation": self.explain(),
        }


def compute_risk_reward(
    entry: float | None, target: float | None, invalidation: float | None,
    direction: str = "LONG",
) -> float | None:
    """Reward-to-risk ratio. ``None`` when the levels are incoherent."""
    if not (is_available(entry) and is_available(target) and is_available(invalidation)):
        return None
    if direction.upper() == "SHORT":
        reward, risk = entry - target, invalidation - entry
    else:
        reward, risk = target - entry, entry - invalidation
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def size_position(
    inputs: SizingInputs, *, config: dict[str, Any] | None = None
) -> SizingResult:
    """Run the sizing pipeline and return a fully-explained recommendation."""
    risk_cfg = config or load_yaml_config("risk")
    limits = risk_cfg.get("limits", {})
    sizing_cfg = risk_cfg.get("sizing", {})
    speculative = set(
        (risk_cfg.get("strategy_classification", {}) or {}).get("speculative", [])
    )

    base_weight = float(sizing_cfg.get("base_weight", 0.06))
    conviction_floor = float(sizing_cfg.get("conviction_floor", 4.0))
    conviction_ref = float(sizing_cfg.get("conviction_reference", 7.0))
    conviction_max = float(sizing_cfg.get("conviction_max_multiplier", 1.8))
    vol_ref = float(sizing_cfg.get("volatility_reference_annual", 0.30))
    vol_min = float(sizing_cfg.get("volatility_min_multiplier", 0.45))
    vol_max = float(sizing_cfg.get("volatility_max_multiplier", 1.35))
    correlation_penalty = float(sizing_cfg.get("correlation_penalty", 0.25))
    min_risk_reward = float(sizing_cfg.get("min_risk_reward", 1.5))

    max_position = float(limits.get("max_position_weight", 0.20))
    max_sector = float(limits.get("max_sector_weight", 0.30))
    min_cash = float(limits.get("min_cash_weight", 0.05))
    max_speculative = float(limits.get("max_speculative_weight", 0.15))
    max_risk = float(limits.get("max_risk_per_position", 0.02))
    min_position = float(limits.get("min_position_weight", 0.01))
    max_adv = float(limits.get("max_single_stock_adv_participation", 0.10))

    result = SizingResult(ticker=inputs.ticker, recommended_weight=0.0)
    steps = result.steps

    # --- Gate 1: conviction floor -------------------------------------------
    if inputs.conviction is None:
        result.rejected = True
        result.rejection_reason = (
            "No conviction score is available, so a size cannot be justified."
        )
        return result
    if inputs.conviction < conviction_floor:
        result.rejected = True
        result.rejection_reason = (
            f"Conviction {inputs.conviction:.1f}/10 is below the {conviction_floor:.1f} "
            "floor required to open a position."
        )
        return result

    # --- Gate 2: risk/reward -------------------------------------------------
    rr = compute_risk_reward(
        inputs.entry_price, inputs.target_price, inputs.invalidation_price, inputs.direction
    )
    result.risk_reward = rr
    if rr is not None and rr < min_risk_reward:
        result.rejected = True
        result.rejection_reason = (
            f"Risk/reward of {rr:.2f}:1 is below the {min_risk_reward:.1f}:1 minimum. "
            "The target does not compensate for the distance to invalidation."
        )
        return result

    # --- Stage 1: base -------------------------------------------------------
    weight = base_weight
    steps.append(SizingStep(
        "base_allocation", base_weight, base_weight,
        f"Base allocation of {base_weight:.1%} before adjustment.",
    ))

    # --- Stage 2: conviction -------------------------------------------------
    multiplier = min(conviction_max, inputs.conviction / conviction_ref)
    before, weight = weight, weight * multiplier
    steps.append(SizingStep(
        "conviction", before, weight,
        f"Conviction {inputs.conviction:.1f}/10 against a {conviction_ref:.0f}/10 "
        f"reference scales the position by {multiplier:.2f}x.",
    ))

    # --- Stage 3: volatility -------------------------------------------------
    if is_available(inputs.annual_volatility) and inputs.annual_volatility > 0:
        vol_multiplier = max(vol_min, min(vol_max, vol_ref / inputs.annual_volatility))
        before, weight = weight, weight * vol_multiplier
        direction_word = "reduces" if vol_multiplier < 1 else "increases"
        steps.append(SizingStep(
            "volatility", before, weight,
            f"Annualised volatility of {inputs.annual_volatility:.0%} versus a "
            f"{vol_ref:.0%} reference {direction_word} the position "
            f"({vol_multiplier:.2f}x).",
        ))
    else:
        steps.append(SizingStep(
            "volatility", weight, weight,
            "Volatility unavailable; no volatility adjustment applied.",
        ))

    # --- Stage 4: risk to invalidation --------------------------------------
    # The real constraint: how much of the portfolio is lost if the thesis breaks.
    if is_available(inputs.entry_price) and is_available(inputs.invalidation_price):
        distance = safe_div(
            abs(inputs.entry_price - inputs.invalidation_price), inputs.entry_price
        )
        if is_available(distance) and distance > 0:
            max_weight_by_risk = max_risk / distance
            result.risk_per_position = weight * distance
            if weight > max_weight_by_risk:
                before, weight = weight, max_weight_by_risk
                steps.append(SizingStep(
                    "risk_per_position", before, weight,
                    f"Invalidation is {distance:.1%} below entry; capping at "
                    f"{max_weight_by_risk:.1%} keeps the loss at or under the "
                    f"{max_risk:.1%} per-position risk limit.",
                ))
                result.risk_per_position = weight * distance
            else:
                steps.append(SizingStep(
                    "risk_per_position", weight, weight,
                    f"Risk to invalidation is {weight * distance:.2%} of the "
                    f"portfolio, within the {max_risk:.1%} limit.",
                ))
    else:
        steps.append(SizingStep(
            "risk_per_position", weight, weight,
            "No invalidation level set; per-position risk could not be bounded.",
        ))

    # --- Stage 5: correlation ------------------------------------------------
    if inputs.correlated_holdings > 0:
        factor = max(0.3, 1.0 - correlation_penalty * inputs.correlated_holdings)
        before, weight = weight, weight * factor
        steps.append(SizingStep(
            "correlation", before, weight,
            f"{inputs.correlated_holdings} existing holding(s) correlated with this "
            f"name reduce the position by {(1 - factor):.0%}.",
        ))

    # --- Stage 6: sector headroom -------------------------------------------
    sector_headroom = max(0.0, max_sector - inputs.current_sector_weight)
    if weight > sector_headroom:
        before, weight = weight, sector_headroom
        steps.append(SizingStep(
            "sector_limit", before, weight,
            f"Sector exposure is already {inputs.current_sector_weight:.1%} against a "
            f"{max_sector:.0%} cap, leaving {sector_headroom:.1%} of headroom.",
        ))

    # --- Stage 7: single-position cap ---------------------------------------
    if weight > max_position:
        before, weight = weight, max_position
        steps.append(SizingStep(
            "position_limit", before, weight,
            f"Capped at the {max_position:.0%} maximum single-position weight.",
        ))

    # --- Stage 8: speculative bucket ----------------------------------------
    if inputs.strategy in speculative:
        headroom = max(0.0, max_speculative - inputs.current_speculative_weight)
        if weight > headroom:
            before, weight = weight, headroom
            steps.append(SizingStep(
                "speculative_limit", before, weight,
                f"'{inputs.strategy}' counts as speculative; exposure is "
                f"{inputs.current_speculative_weight:.1%} against a "
                f"{max_speculative:.0%} cap.",
            ))

    # --- Stage 9: cash floor -------------------------------------------------
    investable = max(0.0, inputs.current_cash_weight - min_cash)
    if weight > investable:
        before, weight = weight, investable
        steps.append(SizingStep(
            "cash_floor", before, weight,
            f"Only {investable:.1%} is investable after reserving the "
            f"{min_cash:.0%} minimum cash buffer.",
        ))

    # --- Stage 10: liquidity -------------------------------------------------
    if is_available(inputs.average_turnover) and inputs.portfolio_value:
        max_value = inputs.average_turnover * max_adv
        max_weight_by_liquidity = max_value / inputs.portfolio_value
        if weight > max_weight_by_liquidity:
            before, weight = weight, max_weight_by_liquidity
            steps.append(SizingStep(
                "liquidity", before, weight,
                f"Average daily turnover of EGP {inputs.average_turnover:,.0f} limits "
                f"the position to {max_adv:.0%} of daily volume — an exit must be "
                "achievable without moving the price.",
            ))

    # --- Final: viability ----------------------------------------------------
    if weight < min_position:
        result.rejected = True
        result.rejection_reason = (
            f"After all constraints the position would be {weight:.2%}, below the "
            f"{min_position:.0%} minimum. A position this small is not worth the "
            "transaction costs."
        )
        result.recommended_weight = 0.0
        return result

    result.recommended_weight = weight
    if inputs.portfolio_value:
        result.recommended_value = weight * inputs.portfolio_value
        if inputs.entry_price and inputs.entry_price > 0:
            result.recommended_quantity = int(result.recommended_value // inputs.entry_price)
    if is_available(inputs.entry_price) and is_available(inputs.invalidation_price):
        distance = safe_div(
            abs(inputs.entry_price - inputs.invalidation_price), inputs.entry_price
        )
        if is_available(distance):
            result.risk_per_position = weight * distance
    return result
