"""Technical indicators, verified against hand-computed values."""
from __future__ import annotations

import math

import pytest

from backend.analytics.indicators import (
    atr,
    bollinger_bands,
    drawdown_series,
    ema,
    last_valid,
    macd,
    momentum,
    relative_strength,
    rolling_volatility,
    rsi,
    sma,
    support_resistance,
    true_range,
)


class TestSMA:
    def test_known_values(self):
        assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]

    def test_leading_none_until_window_full(self):
        result = sma(list(range(1, 11)), 5)
        assert result[:4] == [None] * 4
        assert result[4] == 3.0

    def test_gap_resets_window_rather_than_bridging(self):
        # Bridging a gap would invent a value across a hole in the data.
        assert sma([1, 2, 3, None, 5, 6, 7, 8], 3) == [
            None, None, 2.0, None, None, None, 6.0, 7.0
        ]

    def test_period_longer_than_series(self):
        assert sma([1, 2], 5) == [None, None]

    def test_empty(self):
        assert sma([], 3) == []

    def test_zero_period_rejected(self):
        with pytest.raises(ValueError):
            sma([1, 2, 3], 0)


class TestEMA:
    def test_seeded_with_sma_then_smooths(self):
        # Seed = SMA(1,2,3) = 2; then 2+(4-2)*0.5=3; 3+(5-3)*0.5=4
        assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]

    def test_reacts_faster_than_sma(self):
        values = [10] * 20 + [20] * 5
        assert last_valid(ema(values, 10)) > last_valid(sma(values, 10))


class TestRSI:
    def test_pure_uptrend_is_100(self):
        assert last_valid(rsi(list(range(1, 40)), 14)) == pytest.approx(100.0)

    def test_pure_downtrend_is_zero(self):
        assert last_valid(rsi(list(range(40, 1, -1)), 14)) == pytest.approx(0.0)

    def test_bounded_0_100(self):
        values = [10, 12, 11, 15, 13, 18, 16, 20, 19, 24, 22, 27, 25, 30, 28, 33]
        for value in rsi(values, 5):
            if value is not None:
                assert 0.0 <= value <= 100.0

    def test_insufficient_data(self):
        assert rsi([1, 2], 14) == [None, None]


class TestMACD:
    def test_needs_slow_period_bars(self):
        line, signal, hist = macd(list(range(1, 60)))
        assert sum(1 for v in line if v is None) == 25   # 26-period slow EMA
        assert line[-1] is not None and signal[-1] is not None and hist[-1] is not None

    def test_histogram_is_line_minus_signal(self):
        line, signal, hist = macd(list(range(1, 80)))
        for l, s, h in zip(line, signal, hist):
            if l is not None and s is not None:
                assert h == pytest.approx(l - s)

    def test_signal_not_contaminated_by_leading_nones(self):
        # The signal EMA must run only over the defined MACD region.
        line, signal, _ = macd([100 + i * 0.5 for i in range(80)])
        first_line = next(i for i, v in enumerate(line) if v is not None)
        first_signal = next(i for i, v in enumerate(signal) if v is not None)
        assert first_signal >= first_line + 8


class TestATR:
    def test_true_range_uses_prior_close(self):
        tr = true_range([10, 12], [8, 11], [9, 11.5])
        assert tr[0] == 2.0                    # first bar: high - low
        assert tr[1] == pytest.approx(3.0)     # max(1, |12-9|, |11-9|)

    def test_atr_positive(self):
        highs = [10 + i * 0.1 for i in range(30)]
        lows = [9 + i * 0.1 for i in range(30)]
        closes = [9.5 + i * 0.1 for i in range(30)]
        assert last_valid(atr(highs, lows, closes, 14)) > 0


class TestBollinger:
    def test_flat_series_collapses_bands(self):
        upper, mid, lower = bollinger_bands([10] * 25, 20)
        assert upper[-1] == mid[-1] == lower[-1] == 10.0

    def test_bands_straddle_mean(self):
        values = [10, 11, 9, 12, 8, 13, 7, 14, 6, 15] * 3
        upper, mid, lower = bollinger_bands(values, 20)
        assert upper[-1] > mid[-1] > lower[-1]


class TestVolatilityAndMomentum:
    def test_flat_series_zero_volatility(self):
        assert last_valid(rolling_volatility([10] * 30, 20)) == pytest.approx(0.0)

    def test_annualization_factor(self):
        values = [100 * (1.01 ** i) if i % 2 else 100 * (0.99 ** i) for i in range(60)]
        daily = last_valid(rolling_volatility(values, 20, annualize=False))
        annual = last_valid(rolling_volatility(values, 20, annualize=True))
        assert annual == pytest.approx(daily * math.sqrt(252), rel=1e-6)

    def test_momentum(self):
        assert momentum([100] * 63 + [110], 63)[-1] == pytest.approx(0.10)

    def test_momentum_insufficient_history(self):
        assert momentum([100, 101], 63) == [None, None]

    def test_relative_strength_is_excess(self):
        stock = [100] * 63 + [115]
        bench = [100] * 63 + [105]
        assert relative_strength(stock, bench, 63)[-1] == pytest.approx(0.10)


class TestSupportResistance:
    def test_finds_levels_around_price(self):
        highs, lows, closes = [], [], []
        for i in range(120):
            base = 100 + 10 * math.sin(i / 6.0)
            highs.append(base + 1)
            lows.append(base - 1)
            closes.append(base)
        support, resistance = support_resistance(highs, lows, closes, pivot_window=3)
        assert support or resistance
        for level in support:
            assert level < closes[-1]
        for level in resistance:
            assert level > closes[-1]

    def test_too_short_series(self):
        assert support_resistance([1, 2], [1, 2], [1, 2]) == ([], [])


class TestDrawdown:
    def test_known_values(self):
        result = drawdown_series([100, 110, 99, 88, 120])
        assert result[0] == 0.0
        assert result[2] == pytest.approx(-0.10)
        assert result[3] == pytest.approx(-0.20)
        assert result[4] == 0.0


def test_all_indicators_preserve_length():
    values = [100 + i for i in range(50)]
    for series in (sma(values, 10), ema(values, 10), rsi(values, 14),
                   rolling_volatility(values, 20), momentum(values, 20)):
        assert len(series) == len(values)
