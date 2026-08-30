"""Technical analysis engine.

Turns raw price series into indicators, named signals, and a 0-100 technical
score. Every score component reports the inputs that produced it, so the number
is always auditable back to the bars it came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from backend.analytics import indicators as ind
from backend.analytics.scoring import ScoreComponent, ScoreResult, build_score
from backend.core.config import load_yaml_config
from backend.core.data_quality import Confidence

#: Bars required before a technical read is meaningful at all.
MIN_BARS = 30
#: Bars required for the long-term trend components (SMA200).
FULL_BARS = 200


@dataclass
class Signal:
    """A named, detected technical condition."""

    name: str
    direction: str          # bullish | bearish | neutral
    strength: float         # 0..1
    description: str
    detected_on: date | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction,
            "strength": round(self.strength, 3),
            "description": self.description,
            "detected_on": self.detected_on.isoformat() if self.detected_on else None,
        }


@dataclass
class TechnicalSnapshot:
    """Current technical state of one instrument."""

    ticker: str
    as_of: date | None = None
    bars_available: int = 0
    price: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    sma100: float | None = None
    sma200: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    bb_position: float | None = None      # 0 = lower band, 1 = upper band
    volume: float | None = None
    volume_sma20: float | None = None
    volume_ratio: float | None = None
    volatility_20d: float | None = None
    momentum_1m: float | None = None
    momentum_3m: float | None = None
    momentum_6m: float | None = None
    momentum_12m: float | None = None
    relative_strength_3m: float | None = None
    support_levels: list[float] = field(default_factory=list)
    resistance_levels: list[float] = field(default_factory=list)
    trend: str = "UNKNOWN"
    signals: list[Signal] = field(default_factory=list)
    score: ScoreResult | None = None
    insufficient_data: bool = False
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "bars_available": self.bars_available,
            "insufficient_data": self.insufficient_data,
            "note": self.note,
            "price": self.price,
            "sma20": self.sma20, "sma50": self.sma50,
            "sma100": self.sma100, "sma200": self.sma200,
            "ema20": self.ema20, "ema50": self.ema50,
            "rsi14": self.rsi14,
            "macd": self.macd, "macd_signal": self.macd_signal, "macd_hist": self.macd_hist,
            "atr14": self.atr14, "atr_pct": self.atr_pct,
            "bb_upper": self.bb_upper, "bb_middle": self.bb_middle,
            "bb_lower": self.bb_lower, "bb_position": self.bb_position,
            "volume": self.volume, "volume_sma20": self.volume_sma20,
            "volume_ratio": self.volume_ratio,
            "volatility_20d": self.volatility_20d,
            "momentum_1m": self.momentum_1m, "momentum_3m": self.momentum_3m,
            "momentum_6m": self.momentum_6m, "momentum_12m": self.momentum_12m,
            "relative_strength_3m": self.relative_strength_3m,
            "support_levels": self.support_levels,
            "resistance_levels": self.resistance_levels,
            "trend": self.trend,
            "signals": [s.to_dict() for s in self.signals],
            "score": self.score.to_dict() if self.score else None,
        }


def analyze_technical(
    ticker: str,
    dates: Sequence[date],
    opens: Sequence[float | None],
    highs: Sequence[float | None],
    lows: Sequence[float | None],
    closes: Sequence[float | None],
    volumes: Sequence[float | None],
    *,
    benchmark_closes: Sequence[float | None] | None = None,
    weights: dict[str, float] | None = None,
) -> TechnicalSnapshot:
    """Compute the full technical picture for one instrument."""
    snapshot = TechnicalSnapshot(ticker=ticker, bars_available=len(closes))
    snapshot.as_of = dates[-1] if dates else None

    if len(closes) < MIN_BARS:
        snapshot.insufficient_data = True
        snapshot.note = (
            f"Only {len(closes)} price bars available; at least {MIN_BARS} are "
            "required for a technical read. No technical score computed."
        )
        return snapshot

    last = ind.last_valid
    snapshot.price = last(closes)
    snapshot.sma20 = last(ind.sma(closes, 20))
    snapshot.sma50 = last(ind.sma(closes, 50))
    snapshot.sma100 = last(ind.sma(closes, 100))
    snapshot.sma200 = last(ind.sma(closes, 200))
    snapshot.ema20 = last(ind.ema(closes, 20))
    snapshot.ema50 = last(ind.ema(closes, 50))

    rsi_series = ind.rsi(closes, 14)
    snapshot.rsi14 = last(rsi_series)

    macd_line, signal_line, hist = ind.macd(closes)
    snapshot.macd = last(macd_line)
    snapshot.macd_signal = last(signal_line)
    snapshot.macd_hist = last(hist)

    atr_series = ind.atr(highs, lows, closes, 14)
    snapshot.atr14 = last(atr_series)
    if snapshot.atr14 and snapshot.price:
        snapshot.atr_pct = snapshot.atr14 / snapshot.price

    upper, middle, lower = ind.bollinger_bands(closes, 20)
    snapshot.bb_upper, snapshot.bb_middle, snapshot.bb_lower = last(upper), last(middle), last(lower)
    if snapshot.bb_upper and snapshot.bb_lower and snapshot.price is not None:
        width = snapshot.bb_upper - snapshot.bb_lower
        snapshot.bb_position = (snapshot.price - snapshot.bb_lower) / width if width > 0 else 0.5

    snapshot.volume = last(volumes)
    snapshot.volume_sma20 = last(ind.volume_sma(volumes, 20))
    if snapshot.volume is not None and snapshot.volume_sma20:
        snapshot.volume_ratio = snapshot.volume / snapshot.volume_sma20

    snapshot.volatility_20d = last(ind.rolling_volatility(closes, 20))
    snapshot.momentum_1m = last(ind.momentum(closes, 21))
    snapshot.momentum_3m = last(ind.momentum(closes, 63))
    snapshot.momentum_6m = last(ind.momentum(closes, 126))
    snapshot.momentum_12m = last(ind.momentum(closes, 252))

    if benchmark_closes and len(benchmark_closes) == len(closes):
        snapshot.relative_strength_3m = last(
            ind.relative_strength(closes, benchmark_closes, 63)
        )

    snapshot.support_levels, snapshot.resistance_levels = ind.support_resistance(
        highs, lows, closes
    )
    snapshot.trend = _classify_trend(snapshot)
    snapshot.signals = _detect_signals(
        snapshot, dates, closes, volumes, rsi_series, macd_line, signal_line
    )
    snapshot.score = _score_technical(snapshot, weights)
    return snapshot


# ---------------------------------------------------------------------------
# Trend classification
# ---------------------------------------------------------------------------
def _classify_trend(s: TechnicalSnapshot) -> str:
    price, sma50, sma200 = s.price, s.sma50, s.sma200
    if price is None:
        return "UNKNOWN"
    if sma200 is not None and sma50 is not None:
        if price > sma50 > sma200:
            return "STRONG_UPTREND"
        if price < sma50 < sma200:
            return "STRONG_DOWNTREND"
        if price > sma200:
            return "UPTREND"
        return "DOWNTREND"
    if s.sma20 is not None and sma50 is not None:
        if price > s.sma20 > sma50:
            return "UPTREND"
        if price < s.sma20 < sma50:
            return "DOWNTREND"
    return "SIDEWAYS"


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------
def _detect_signals(
    s: TechnicalSnapshot,
    dates: Sequence[date],
    closes: Sequence[float | None],
    volumes: Sequence[float | None],
    rsi_series: Sequence[float | None],
    macd_line: Sequence[float | None],
    signal_line: Sequence[float | None],
) -> list[Signal]:
    signals: list[Signal] = []
    today = dates[-1] if dates else None
    volume_confirmed = (s.volume_ratio or 0) >= 1.5

    # --- Golden / death cross (50 vs 200), checked for a *recent* crossing ---
    sma50_series = ind.sma(closes, 50)
    sma200_series = ind.sma(closes, 200)
    cross = _recent_cross(sma50_series, sma200_series, lookback=10)
    if cross == "above":
        signals.append(Signal(
            "GOLDEN_CROSS", "bullish", 0.85,
            "50-day SMA crossed above the 200-day SMA within the last 10 sessions.",
            today,
        ))
    elif cross == "below":
        signals.append(Signal(
            "DEATH_CROSS", "bearish", 0.85,
            "50-day SMA crossed below the 200-day SMA within the last 10 sessions.",
            today,
        ))

    # --- Breakout / breakdown vs 52-week (or available) range ---------------
    window = [c for c in closes[-252:] if c is not None]
    if window and s.price is not None and len(window) >= 60:
        high, low = max(window), min(window)
        if s.price >= high * 0.995:
            signals.append(Signal(
                "BREAKOUT", "bullish", 0.9 if volume_confirmed else 0.6,
                f"Price at/near the {len(window)}-session high of {high:,.2f}"
                + (" with volume confirmation." if volume_confirmed
                   else " but without volume confirmation."),
                today,
            ))
        elif s.price <= low * 1.005:
            signals.append(Signal(
                "BREAKDOWN", "bearish", 0.9 if volume_confirmed else 0.6,
                f"Price at/near the {len(window)}-session low of {low:,.2f}"
                + (" on elevated volume." if volume_confirmed else "."),
                today,
            ))

    # --- Resistance / support proximity -------------------------------------
    if s.price and s.resistance_levels:
        nearest = min(s.resistance_levels, key=lambda r: abs(r - s.price))
        if 0 < (nearest - s.price) / s.price <= 0.03:
            signals.append(Signal(
                "APPROACHING_RESISTANCE", "neutral", 0.5,
                f"Price is within 3% of resistance at {nearest:,.2f}.", today,
            ))
    if s.price and s.support_levels:
        nearest = min(s.support_levels, key=lambda r: abs(r - s.price))
        if 0 < (s.price - nearest) / s.price <= 0.03:
            signals.append(Signal(
                "AT_SUPPORT", "bullish", 0.55,
                f"Price is within 3% of support at {nearest:,.2f}.", today,
            ))

    # --- RSI extremes --------------------------------------------------------
    if s.rsi14 is not None:
        if s.rsi14 <= 30:
            signals.append(Signal(
                "OVERSOLD", "bullish", 0.6 + min(0.3, (30 - s.rsi14) / 60),
                f"RSI(14) at {s.rsi14:.1f}, in oversold territory.", today,
            ))
        elif s.rsi14 >= 70:
            signals.append(Signal(
                "OVERBOUGHT", "bearish", 0.6 + min(0.3, (s.rsi14 - 70) / 60),
                f"RSI(14) at {s.rsi14:.1f}, in overbought territory.", today,
            ))

    # --- MACD crossover ------------------------------------------------------
    macd_cross = _recent_cross(macd_line, signal_line, lookback=5)
    if macd_cross == "above":
        signals.append(Signal(
            "MACD_BULLISH_CROSS", "bullish", 0.7,
            "MACD line crossed above its signal line within the last 5 sessions.", today,
        ))
    elif macd_cross == "below":
        signals.append(Signal(
            "MACD_BEARISH_CROSS", "bearish", 0.7,
            "MACD line crossed below its signal line within the last 5 sessions.", today,
        ))

    # --- Volume confirmation -------------------------------------------------
    if s.volume_ratio and s.volume_ratio >= 2.0:
        recent = [c for c in closes[-2:] if c is not None]
        direction = "bullish" if len(recent) == 2 and recent[-1] > recent[0] else "bearish"
        signals.append(Signal(
            "VOLUME_SPIKE", direction, min(0.9, 0.4 + s.volume_ratio / 10),
            f"Volume {s.volume_ratio:.1f}x its 20-day average.", today,
        ))

    # --- Momentum acceleration / deterioration ------------------------------
    if s.momentum_1m is not None and s.momentum_3m is not None:
        monthly_pace_3m = s.momentum_3m / 3.0
        if s.momentum_1m > monthly_pace_3m and s.momentum_1m > 0.02:
            signals.append(Signal(
                "MOMENTUM_ACCELERATION", "bullish", 0.65,
                f"1-month return ({s.momentum_1m:+.1%}) is outpacing the 3-month "
                f"average monthly pace ({monthly_pace_3m:+.1%}).", today,
            ))
        elif s.momentum_1m < monthly_pace_3m and s.momentum_1m < -0.02:
            signals.append(Signal(
                "MOMENTUM_DETERIORATION", "bearish", 0.65,
                f"1-month return ({s.momentum_1m:+.1%}) is lagging the 3-month "
                f"average monthly pace ({monthly_pace_3m:+.1%}).", today,
            ))

    # --- Trend change --------------------------------------------------------
    trend_flip = _recent_cross(closes, sma50_series, lookback=5)
    if trend_flip == "above":
        signals.append(Signal(
            "TREND_CHANGE_UP", "bullish", 0.6,
            "Price reclaimed its 50-day SMA within the last 5 sessions.", today,
        ))
    elif trend_flip == "below":
        signals.append(Signal(
            "TREND_CHANGE_DOWN", "bearish", 0.6,
            "Price lost its 50-day SMA within the last 5 sessions.", today,
        ))

    return signals


def _recent_cross(
    fast: Sequence[float | None], slow: Sequence[float | None], lookback: int = 5
) -> str | None:
    """Detect a crossing of *fast* over/under *slow* within *lookback* bars."""
    pairs = [
        (i, f, s)
        for i, (f, s) in enumerate(zip(fast, slow))
        if f is not None and s is not None
    ]
    if len(pairs) < 2:
        return None
    recent = pairs[-(lookback + 1):]
    if len(recent) < 2:
        return None
    for (_, f_prev, s_prev), (_, f_now, s_now) in zip(recent, recent[1:]):
        if f_prev <= s_prev and f_now > s_now:
            return "above"
        if f_prev >= s_prev and f_now < s_now:
            return "below"
    return None


# ---------------------------------------------------------------------------
# Technical score
# ---------------------------------------------------------------------------
def _score_technical(
    s: TechnicalSnapshot, weights: dict[str, float] | None = None
) -> ScoreResult:
    cfg = weights or load_yaml_config("weights").get("technical") or {}
    components: list[ScoreComponent] = []

    # -- Trend (price vs moving averages) ------------------------------------
    trend_points: list[float] = []
    trend_inputs: dict[str, Any] = {}
    for label, ma in (("sma20", s.sma20), ("sma50", s.sma50),
                      ("sma100", s.sma100), ("sma200", s.sma200)):
        if ma and s.price:
            trend_inputs[label] = round(ma, 4)
            trend_points.append(100.0 if s.price > ma else 0.0)
    if s.sma50 and s.sma200:
        trend_points.append(100.0 if s.sma50 > s.sma200 else 0.0)
        trend_inputs["sma50_above_sma200"] = s.sma50 > s.sma200
    components.append(ScoreComponent(
        "trend", cfg.get("trend", 25),
        sum(trend_points) / len(trend_points) if trend_points else None,
        inputs=trend_inputs,
        explanation=(
            f"Price {'above' if trend_points and trend_points[0] > 50 else 'below'} "
            f"its moving averages; trend classified {s.trend}."
            if trend_points else "No moving averages available."
        ),
    ))

    # -- Momentum -------------------------------------------------------------
    mom_values = [
        (s.momentum_1m, 0.20), (s.momentum_3m, 0.35),
        (s.momentum_6m, 0.25), (s.momentum_12m, 0.20),
    ]
    available = [(v, w) for v, w in mom_values if v is not None]
    momentum_score = None
    if available:
        total_w = sum(w for _, w in available)
        blended = sum(v * w for v, w in available) / total_w
        # ±40% over the blended horizon maps to the full 0-100 range.
        momentum_score = max(0.0, min(100.0, 50.0 + (blended / 0.40) * 50.0))
    components.append(ScoreComponent(
        "momentum", cfg.get("momentum", 20), momentum_score,
        inputs={
            "1m": _pct(s.momentum_1m), "3m": _pct(s.momentum_3m),
            "6m": _pct(s.momentum_6m), "12m": _pct(s.momentum_12m),
        },
        explanation="Blended 1/3/6/12-month price returns, weighted toward 3-month.",
    ))

    # -- RSI positioning ------------------------------------------------------
    # Rewards constructive strength (50-65). Penalises both extremes: overbought
    # is late, deeply oversold means something is wrong.
    rsi_score = None
    if s.rsi14 is not None:
        rsi_score = max(0.0, 100.0 - abs(s.rsi14 - 57.5) * 2.5)
    components.append(ScoreComponent(
        "rsi_position", cfg.get("rsi_position", 10), rsi_score,
        inputs={"rsi14": _round(s.rsi14)},
        explanation="Peak score in the constructive 50-65 RSI band; extremes penalised.",
    ))

    # -- MACD -----------------------------------------------------------------
    macd_score = None
    if s.macd is not None and s.macd_signal is not None and s.price:
        spread = (s.macd - s.macd_signal) / s.price
        macd_score = max(0.0, min(100.0, 50.0 + spread * 5000.0))
    components.append(ScoreComponent(
        "macd", cfg.get("macd", 10), macd_score,
        inputs={"macd": _round(s.macd), "signal": _round(s.macd_signal),
                "histogram": _round(s.macd_hist)},
        explanation="MACD spread over signal line, normalised by price.",
    ))

    # -- Volume confirmation --------------------------------------------------
    volume_score = None
    if s.volume_ratio is not None:
        volume_score = max(0.0, min(100.0, 40.0 + (s.volume_ratio - 1.0) * 60.0))
    components.append(ScoreComponent(
        "volume_confirmation", cfg.get("volume_confirmation", 10), volume_score,
        inputs={"volume_ratio": _round(s.volume_ratio)},
        explanation="Current volume relative to its 20-day average.",
    ))

    # -- Relative strength vs benchmark --------------------------------------
    rs_score = None
    if s.relative_strength_3m is not None:
        rs_score = max(0.0, min(100.0, 50.0 + (s.relative_strength_3m / 0.25) * 50.0))
    components.append(ScoreComponent(
        "relative_strength", cfg.get("relative_strength", 15), rs_score,
        inputs={"excess_3m": _pct(s.relative_strength_3m)},
        explanation="3-month return in excess of the benchmark.",
    ))

    # -- Volatility posture (lower is better, to a point) ---------------------
    vol_score = None
    if s.volatility_20d is not None:
        vol_score = max(0.0, min(100.0, 100.0 - (s.volatility_20d / 0.60) * 100.0))
    components.append(ScoreComponent(
        "volatility_posture", cfg.get("volatility_posture", 5), vol_score,
        inputs={"annualised_vol_20d": _pct(s.volatility_20d)},
        explanation="Lower realised volatility scores higher; 60%+ annualised scores zero.",
    ))

    # -- Location within support/resistance range -----------------------------
    sr_score = None
    if s.price and (s.support_levels or s.resistance_levels):
        support = max(s.support_levels) if s.support_levels else None
        resistance = min(s.resistance_levels) if s.resistance_levels else None
        if support and resistance and resistance > support:
            position = (s.price - support) / (resistance - support)
            # Closer to support = better risk/reward on a long.
            sr_score = max(0.0, min(100.0, 100.0 - position * 100.0))
        elif resistance:
            sr_score = 35.0   # below resistance with no defined support
        elif support:
            sr_score = 70.0   # above support with no overhead resistance found
    components.append(ScoreComponent(
        "support_resistance", cfg.get("support_resistance", 5), sr_score,
        inputs={"support": s.support_levels, "resistance": s.resistance_levels},
        explanation="Position within the nearest support/resistance band; nearer support scores higher.",
    ))

    return build_score("technical", components)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _pct(value: float | None) -> str | None:
    return None if value is None else f"{value:+.2%}"
