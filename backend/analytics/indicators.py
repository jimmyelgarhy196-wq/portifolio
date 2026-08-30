"""Technical indicators.

Pure functions over price series. No I/O, no database, no configuration — which
makes every one of them directly testable against known values.

Convention: every function returns a list the same length as its input, with
``None`` in positions where the indicator is not yet defined (e.g. the first 19
values of a 20-period SMA). Returning ``None`` rather than back-filling is what
prevents an indicator from implying knowledge it does not have — and it is also
what makes look-ahead bias impossible here.
"""
from __future__ import annotations

import math
from typing import Sequence

Number = float | None


def _clean(values: Sequence[Number]) -> list[float | None]:
    out: list[float | None] = []
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out.append(None)
        else:
            out.append(float(v))
    return out


def sma(values: Sequence[Number], period: int) -> list[Number]:
    """Simple moving average. ``None`` until *period* valid observations exist."""
    if period <= 0:
        raise ValueError("period must be positive")
    data = _clean(values)
    out: list[Number] = [None] * len(data)
    window: list[float] = []
    for i, value in enumerate(data):
        if value is None:
            window.clear()  # a gap invalidates the window rather than bridging it
            continue
        window.append(value)
        if len(window) > period:
            window.pop(0)
        if len(window) == period:
            out[i] = sum(window) / period
    return out


def ema(values: Sequence[Number], period: int) -> list[Number]:
    """Exponential moving average, seeded with the first full SMA."""
    if period <= 0:
        raise ValueError("period must be positive")
    data = _clean(values)
    out: list[Number] = [None] * len(data)
    multiplier = 2.0 / (period + 1)
    prev: float | None = None
    seed: list[float] = []

    for i, value in enumerate(data):
        if value is None:
            continue
        if prev is None:
            seed.append(value)
            if len(seed) == period:
                prev = sum(seed) / period
                out[i] = prev
            continue
        prev = (value - prev) * multiplier + prev
        out[i] = prev
    return out


def rsi(values: Sequence[Number], period: int = 14) -> list[Number]:
    """Wilder's RSI. Bounded [0, 100]; 100 when there are no losses in the window."""
    data = _clean(values)
    out: list[Number] = [None] * len(data)
    gains: list[float] = []
    losses: list[float] = []
    avg_gain = avg_loss = None
    prev: float | None = None
    count = 0

    for i, value in enumerate(data):
        if value is None:
            continue
        if prev is None:
            prev = value
            continue
        change = value - prev
        prev = value
        gain, loss = max(change, 0.0), max(-change, 0.0)

        if avg_gain is None:
            gains.append(gain)
            losses.append(loss)
            count += 1
            if count == period:
                avg_gain = sum(gains) / period
                avg_loss = sum(losses) / period
                out[i] = _rsi_value(avg_gain, avg_loss)
            continue

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: Sequence[Number], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[Number], list[Number], list[Number]]:
    """Returns (macd_line, signal_line, histogram)."""
    fast_ema, slow_ema = ema(values, fast), ema(values, slow)
    macd_line: list[Number] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]
    # The signal EMA must run only over the defined portion of the MACD line,
    # otherwise its seed would be contaminated by the leading None region.
    defined = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    signal_line: list[Number] = [None] * len(macd_line)
    if defined:
        smoothed = ema([v for _, v in defined], signal)
        for (idx, _), value in zip(defined, smoothed):
            signal_line[idx] = value

    histogram: list[Number] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, histogram


def true_range(
    highs: Sequence[Number], lows: Sequence[Number], closes: Sequence[Number]
) -> list[Number]:
    h, l, c = _clean(highs), _clean(lows), _clean(closes)
    out: list[Number] = [None] * len(c)
    for i in range(len(c)):
        if h[i] is None or l[i] is None:
            continue
        if i == 0 or c[i - 1] is None:
            out[i] = h[i] - l[i]
        else:
            prev_close = c[i - 1]
            out[i] = max(h[i] - l[i], abs(h[i] - prev_close), abs(l[i] - prev_close))
    return out


def atr(
    highs: Sequence[Number], lows: Sequence[Number], closes: Sequence[Number], period: int = 14
) -> list[Number]:
    """Average True Range (Wilder smoothing)."""
    tr = true_range(highs, lows, closes)
    out: list[Number] = [None] * len(tr)
    window: list[float] = []
    prev: float | None = None
    for i, value in enumerate(tr):
        if value is None:
            continue
        if prev is None:
            window.append(value)
            if len(window) == period:
                prev = sum(window) / period
                out[i] = prev
            continue
        prev = (prev * (period - 1) + value) / period
        out[i] = prev
    return out


def bollinger_bands(
    values: Sequence[Number], period: int = 20, num_std: float = 2.0
) -> tuple[list[Number], list[Number], list[Number]]:
    """Returns (upper, middle, lower)."""
    data = _clean(values)
    middle = sma(data, period)
    upper: list[Number] = [None] * len(data)
    lower: list[Number] = [None] * len(data)
    for i in range(len(data)):
        if middle[i] is None:
            continue
        window = [v for v in data[max(0, i - period + 1) : i + 1] if v is not None]
        if len(window) < period:
            continue
        mean = middle[i]
        variance = sum((v - mean) ** 2 for v in window) / period
        deviation = math.sqrt(variance)
        upper[i] = mean + num_std * deviation
        lower[i] = mean - num_std * deviation
    return upper, middle, lower


def rolling_volatility(
    values: Sequence[Number], period: int = 20, annualize: bool = True
) -> list[Number]:
    """Standard deviation of daily log returns, optionally annualised."""
    data = _clean(values)
    returns: list[Number] = [None] * len(data)
    for i in range(1, len(data)):
        prev, curr = data[i - 1], data[i]
        if prev is None or curr is None or prev <= 0 or curr <= 0:
            continue
        returns[i] = math.log(curr / prev)

    out: list[Number] = [None] * len(data)
    factor = math.sqrt(252.0) if annualize else 1.0
    for i in range(len(data)):
        window = [r for r in returns[max(0, i - period + 1) : i + 1] if r is not None]
        if len(window) < max(2, period // 2):
            continue
        mean = sum(window) / len(window)
        variance = sum((r - mean) ** 2 for r in window) / (len(window) - 1)
        out[i] = math.sqrt(variance) * factor
    return out


def momentum(values: Sequence[Number], period: int = 63) -> list[Number]:
    """Price return over *period* bars, as a decimal fraction."""
    data = _clean(values)
    out: list[Number] = [None] * len(data)
    for i in range(len(data)):
        j = i - period
        if j < 0:
            continue
        past, curr = data[j], data[i]
        if past is None or curr is None or past <= 0:
            continue
        out[i] = (curr - past) / past
    return out


def relative_strength(
    values: Sequence[Number], benchmark: Sequence[Number], period: int = 63
) -> list[Number]:
    """Excess return versus a benchmark over *period* bars."""
    stock_mom = momentum(values, period)
    bench_mom = momentum(benchmark, period)
    return [
        (s - b) if (s is not None and b is not None) else None
        for s, b in zip(stock_mom, bench_mom)
    ]


def volume_sma(volumes: Sequence[Number], period: int = 20) -> list[Number]:
    return sma(volumes, period)


def support_resistance(
    highs: Sequence[Number],
    lows: Sequence[Number],
    closes: Sequence[Number],
    *,
    lookback: int = 120,
    pivot_window: int = 5,
    max_levels: int = 3,
) -> tuple[list[float], list[float]]:
    """Identify support/resistance from clustered swing pivots.

    A pivot is a local extreme with *pivot_window* bars either side. Nearby
    pivots are clustered so a level tested repeatedly counts once, which is what
    makes it a level rather than a coincidence.
    """
    h, l, c = _clean(highs), _clean(lows), _clean(closes)
    h, l, c = h[-lookback:], l[-lookback:], c[-lookback:]
    n = len(c)
    if n < pivot_window * 2 + 1:
        return [], []

    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for i in range(pivot_window, n - pivot_window):
        window = slice(i - pivot_window, i + pivot_window + 1)
        hs = [v for v in h[window] if v is not None]
        ls = [v for v in l[window] if v is not None]
        if h[i] is not None and hs and h[i] >= max(hs):
            swing_highs.append(h[i])
        if l[i] is not None and ls and l[i] <= min(ls):
            swing_lows.append(l[i])

    last = next((v for v in reversed(c) if v is not None), None)
    if last is None:
        return [], []

    resistance = _cluster([v for v in swing_highs if v > last], max_levels)
    support = _cluster([v for v in swing_lows if v < last], max_levels, reverse=True)
    return support, resistance


def _cluster(levels: list[float], max_levels: int, *, tolerance: float = 0.02,
             reverse: bool = False) -> list[float]:
    """Merge levels within *tolerance* of each other; strongest (most tested) first."""
    if not levels:
        return []
    ordered = sorted(levels)
    clusters: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        centre = sum(clusters[-1]) / len(clusters[-1])
        if centre > 0 and abs(value - centre) / centre <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    # Rank by how many pivots formed the cluster — a level tested more is stronger.
    ranked = sorted(clusters, key=len, reverse=True)[:max_levels]
    result = [round(sum(c) / len(c), 4) for c in ranked]
    return sorted(result, reverse=reverse)


def drawdown_series(values: Sequence[Number]) -> list[Number]:
    """Drawdown from running peak, as a negative fraction."""
    data = _clean(values)
    out: list[Number] = [None] * len(data)
    peak: float | None = None
    for i, value in enumerate(data):
        if value is None:
            continue
        peak = value if peak is None else max(peak, value)
        out[i] = (value - peak) / peak if peak > 0 else None
    return out


def last_valid(series: Sequence[Number]) -> Number:
    """Most recent non-None value — the standard way to read 'current' state."""
    for value in reversed(series):
        if value is not None:
            return value
    return None
