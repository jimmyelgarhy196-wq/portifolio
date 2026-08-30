"""Backtest strategies.

A strategy receives a :class:`PointInTimeDataView` and returns target weights.
It cannot see the future, because the view will not serve it — so a strategy is
free to be written naively without accidentally cheating.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from backend.analytics.fundamental import analyze_fundamentals
from backend.analytics.quant import analyze_universe
from backend.analytics.technical import analyze_technical
from backend.backtesting.point_in_time import PointInTimeDataView
from backend.core.data_quality import is_available


@dataclass
class Candidate:
    ticker: str
    score: float
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)


class Strategy(abc.ABC):
    """Base strategy. Subclasses implement :meth:`select`."""

    name = "strategy"
    description = ""

    def __init__(self, **params: Any) -> None:
        self.params = params

    @abc.abstractmethod
    def select(
        self, view: PointInTimeDataView, universe: Sequence[str]
    ) -> list[Candidate]:
        """Rank the universe at this point in time. Highest score first."""

    def target_weights(
        self, view: PointInTimeDataView, universe: Sequence[str]
    ) -> dict[str, float]:
        """Convert the ranking into target portfolio weights."""
        top_n = int(self.params.get("top_n", 10))
        max_weight = float(self.params.get("max_weight", 0.20))
        min_score = self.params.get("min_score")

        candidates = self.select(view, universe)
        if min_score is not None:
            candidates = [c for c in candidates if c.score >= float(min_score)]
        selected = candidates[:top_n]
        if not selected:
            return {}

        if self.params.get("score_weighted"):
            # Weight by score above the selection floor, so conviction shows up
            # in the allocation rather than every name getting the same slice.
            floor = min(c.score for c in selected)
            weights = {c.ticker: max(c.score - floor + 1.0, 1.0) for c in selected}
            total = sum(weights.values())
            raw = {t: w / total for t, w in weights.items()}
        else:
            raw = {c.ticker: 1.0 / len(selected) for c in selected}

        # Apply the per-position cap, then renormalise onto the invested share.
        capped = {t: min(w, max_weight) for t, w in raw.items()}
        invested = float(self.params.get("invested_weight", 0.95))
        total = sum(capped.values())
        if total <= 0:
            return {}
        return {t: (w / total) * invested for t, w in capped.items()}

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "params": self.params}


class FundamentalLongStrategy(Strategy):
    """Buy the cheapest high-quality names, rebalanced periodically.

    Uses only statements that were *published* by the simulation date, so a
    company's full-year numbers are invisible until they were actually filed.
    """

    name = "fundamental_long"
    description = (
        "Ranks on valuation (P/E, EV/EBITDA, FCF yield) and quality (ROE, ROIC, "
        "leverage) from point-in-time published financials."
    )

    def select(self, view: PointInTimeDataView, universe: Sequence[str]) -> list[Candidate]:
        out: list[Candidate] = []
        for ticker in universe:
            periods = view.financial_periods(ticker)
            if not periods:
                continue
            price = view.price(ticker)
            if price is None:
                continue
            company = next(iter(view.companies([ticker])), None)
            snapshot = analyze_fundamentals(
                ticker, periods, price=price,
                shares_outstanding=company.shares_outstanding if company else None,
            )
            if not snapshot.score or not snapshot.score.available:
                continue
            out.append(Candidate(
                ticker=ticker,
                score=snapshot.score.value,
                reason=(
                    f"Fundamental score {snapshot.score.value:.0f} on {snapshot.latest_period} "
                    f"(published {snapshot.available_from})"
                ),
                metrics={
                    "period": snapshot.latest_period,
                    "available_from": (
                        snapshot.available_from.isoformat() if snapshot.available_from else None
                    ),
                    "pe": _metric(snapshot, "pe"),
                    "roe": _metric(snapshot, "roe"),
                },
            ))
        out.sort(key=lambda c: -c.score)
        return out


class MomentumStrategy(Strategy):
    """Classic cross-sectional momentum with a short-term reversal filter."""

    name = "momentum"
    description = (
        "Ranks on 12-month return skipping the most recent month, which avoids "
        "the well-documented short-term reversal effect."
    )

    def select(self, view: PointInTimeDataView, universe: Sequence[str]) -> list[Candidate]:
        skip_days = int(self.params.get("skip_days", 21))
        lookback = int(self.params.get("lookback_days", 252))
        out: list[Candidate] = []

        for ticker in universe:
            series = view.price_series(ticker, lookback_days=lookback + skip_days + 60)
            closes = [c for c in series.closes if c is not None]
            if len(closes) < lookback // 2:
                continue
            recent = closes[-skip_days - 1] if len(closes) > skip_days else closes[-1]
            past_index = max(0, len(closes) - lookback - skip_days)
            past = closes[past_index]
            if not past or past <= 0:
                continue
            momentum = (recent - past) / past
            out.append(Candidate(
                ticker=ticker,
                score=momentum * 100.0,
                reason=f"{lookback}-day momentum of {momentum:+.1%}, skipping {skip_days} days",
                metrics={"momentum": round(momentum, 4)},
            ))
        out.sort(key=lambda c: -c.score)
        return out


class TechnicalSwingStrategy(Strategy):
    """Trend-following: hold names in confirmed uptrends with healthy momentum."""

    name = "technical_swing"
    description = (
        "Requires price above the 50- and 200-day SMAs with RSI in a constructive "
        "band, ranked by the composite technical score."
    )

    def select(self, view: PointInTimeDataView, universe: Sequence[str]) -> list[Candidate]:
        rsi_min = float(self.params.get("rsi_min", 45))
        rsi_max = float(self.params.get("rsi_max", 78))
        out: list[Candidate] = []

        for ticker in universe:
            series = view.price_series(ticker, lookback_days=500)
            if len(series) < 200:
                continue
            technical = analyze_technical(
                ticker, series.dates, series.opens, series.highs,
                series.lows, series.closes, series.volumes,
            )
            if technical.insufficient_data or not technical.score or not technical.score.available:
                continue
            price, sma50, sma200 = technical.price, technical.sma50, technical.sma200
            if not (price and sma50 and sma200):
                continue
            if not (price > sma50 and price > sma200):
                continue
            if technical.rsi14 is None or not (rsi_min <= technical.rsi14 <= rsi_max):
                continue
            out.append(Candidate(
                ticker=ticker,
                score=technical.score.value,
                reason=(
                    f"{technical.trend}, technical score {technical.score.value:.0f}, "
                    f"RSI {technical.rsi14:.0f}"
                ),
                metrics={"trend": technical.trend, "rsi": round(technical.rsi14, 1)},
            ))
        out.sort(key=lambda c: -c.score)
        return out


class MultiFactorStrategy(Strategy):
    """The full cross-sectional factor model, rebalanced periodically."""

    name = "multi_factor"
    description = (
        "Runs the six-factor model (value, momentum, quality, growth, liquidity, "
        "volatility) across the universe and holds the highest composite scores."
    )

    def select(self, view: PointInTimeDataView, universe: Sequence[str]) -> list[Candidate]:
        universe_metrics: dict[str, dict[str, float | None]] = {}

        for ticker in universe:
            series = view.price_series(ticker, lookback_days=500)
            metrics: dict[str, float | None] = {}
            if len(series) >= 60:
                technical = analyze_technical(
                    ticker, series.dates, series.opens, series.highs,
                    series.lows, series.closes, series.volumes,
                )
                metrics.update({
                    "momentum_1m": technical.momentum_1m,
                    "momentum_3m": technical.momentum_3m,
                    "momentum_6m": technical.momentum_6m,
                    "momentum_12m": technical.momentum_12m,
                    "volatility_20d": technical.volatility_20d,
                    "atr_pct": technical.atr_pct,
                })
            pairs = [
                (c, v) for c, v in zip(series.closes[-20:], series.volumes[-20:])
                if c is not None and v is not None
            ]
            metrics["average_turnover"] = (
                sum(c * v for c, v in pairs) / len(pairs) if pairs else None
            )

            periods = view.financial_periods(ticker)
            price = view.price(ticker)
            if periods and price:
                company = next(iter(view.companies([ticker])), None)
                snapshot = analyze_fundamentals(
                    ticker, periods, price=price,
                    shares_outstanding=company.shares_outstanding if company else None,
                )
                for key in (
                    "pe", "pb", "ev_ebitda", "fcf_yield", "roe", "roic", "net_margin",
                    "debt_to_equity", "revenue_growth", "eps_growth", "revenue_cagr",
                    "market_cap",
                ):
                    metrics[key] = _metric(snapshot, key)
            universe_metrics[ticker] = metrics

        snapshots = analyze_universe(universe_metrics)
        out: list[Candidate] = []
        for ticker, snapshot in snapshots.items():
            if snapshot.score and snapshot.score.available:
                factors = {
                    name: round(e.score, 1)
                    for name, e in snapshot.factors.items() if e.score is not None
                }
                out.append(Candidate(
                    ticker=ticker,
                    score=snapshot.score.value,
                    reason=f"Composite factor score {snapshot.score.value:.0f}",
                    metrics=factors,
                ))
        out.sort(key=lambda c: -c.score)
        return out


class BuyAndHoldBenchmark(Strategy):
    """Equal-weight the universe once and hold. The bar every strategy must clear."""

    name = "buy_and_hold"
    description = "Equal-weight buy and hold across the universe."

    def select(self, view: PointInTimeDataView, universe: Sequence[str]) -> list[Candidate]:
        return [
            Candidate(ticker=t, score=1.0, reason="Equal-weight buy and hold")
            for t in universe if view.tradeable(t)
        ]


def _metric(snapshot: Any, key: str) -> float | None:
    metric = snapshot.metrics.get(key)
    return metric.value if metric and metric.available and is_available(metric.value) else None


STRATEGIES: dict[str, type[Strategy]] = {
    FundamentalLongStrategy.name: FundamentalLongStrategy,
    MomentumStrategy.name: MomentumStrategy,
    TechnicalSwingStrategy.name: TechnicalSwingStrategy,
    MultiFactorStrategy.name: MultiFactorStrategy,
    BuyAndHoldBenchmark.name: BuyAndHoldBenchmark,
}


def build_strategy(name: str, **params: Any) -> Strategy:
    cls = STRATEGIES.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown strategy {name!r}. Available: {', '.join(sorted(STRATEGIES))}"
        )
    return cls(**params)
