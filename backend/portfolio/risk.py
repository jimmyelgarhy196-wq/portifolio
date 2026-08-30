"""Portfolio risk engine.

Measures portfolio-level risk and raises warnings when configured limits are
breached. Every measure states what it needs; where an input is missing the
measure is reported as unavailable rather than assumed benign — a risk you
cannot measure is not a risk you do not have.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import load_yaml_config
from backend.core.data_quality import is_available, safe_div
from backend.core.logging_config import EVENT_ALERT, get_logger, log_event
from backend.data.models import Portfolio, PortfolioSnapshot, Position, PriceBar

logger = get_logger(__name__)

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class RiskWarning:
    code: str
    severity: str            # info | warning | critical
    title: str
    message: str
    current: float | None = None
    limit: float | None = None
    ticker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "current": self.current,
            "limit": self.limit,
            "ticker": self.ticker,
        }

    def render(self) -> str:
        icon = {"critical": "⛔", "warning": "⚠", "info": "ℹ"}.get(self.severity, "•")
        return f"{icon} {self.title}\n\n{self.message}"


@dataclass
class RiskReport:
    as_of: date
    portfolio_value: float
    cash_weight: float | None = None
    gross_exposure: float | None = None
    net_exposure: float | None = None
    largest_position: tuple[str, float] | None = None
    position_count: int = 0
    sector_weights: dict[str, float] = field(default_factory=dict)
    strategy_weights: dict[str, float] = field(default_factory=dict)
    portfolio_volatility: float | None = None
    beta: float | None = None
    max_drawdown: float | None = None
    current_drawdown: float | None = None
    value_at_risk_95: float | None = None
    concentration_hhi: float | None = None
    illiquid_positions: list[tuple[str, float]] = field(default_factory=list)
    correlation_clusters: list[list[str]] = field(default_factory=list)
    thesis_risk: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[RiskWarning] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    @property
    def worst_severity(self) -> str:
        if not self.warnings:
            return "info"
        return max(self.warnings, key=lambda w: SEVERITY_ORDER.get(w.severity, 0)).severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "portfolio_value": self.portfolio_value,
            "cash_weight": self.cash_weight,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "largest_position": (
                {"ticker": self.largest_position[0], "weight": self.largest_position[1]}
                if self.largest_position else None
            ),
            "position_count": self.position_count,
            "sector_weights": self.sector_weights,
            "strategy_weights": self.strategy_weights,
            "portfolio_volatility": self.portfolio_volatility,
            "beta": self.beta,
            "max_drawdown": self.max_drawdown,
            "current_drawdown": self.current_drawdown,
            "value_at_risk_95": self.value_at_risk_95,
            "concentration_hhi": self.concentration_hhi,
            "illiquid_positions": [
                {"ticker": t, "days_to_exit": d} for t, d in self.illiquid_positions
            ],
            "correlation_clusters": self.correlation_clusters,
            "thesis_risk": self.thesis_risk,
            "warnings": [w.to_dict() for w in self.warnings],
            "unavailable": self.unavailable,
            "worst_severity": self.worst_severity,
        }


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def returns_from_series(values: Sequence[float | None]) -> list[float]:
    out: list[float] = []
    prev: float | None = None
    for value in values:
        if value is None or value <= 0:
            continue
        if prev is not None:
            out.append((value - prev) / prev)
        prev = value
    return out


def annualized_volatility(returns: Sequence[float], periods_per_year: int = 252) -> float | None:
    if len(returns) < 5:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(periods_per_year)


def compute_beta(
    portfolio_returns: Sequence[float], benchmark_returns: Sequence[float]
) -> float | None:
    """Covariance / benchmark variance over the overlapping window."""
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < 20:
        return None
    p = list(portfolio_returns[-n:])
    b = list(benchmark_returns[-n:])
    p_mean, b_mean = sum(p) / n, sum(b) / n
    covariance = sum((pi - p_mean) * (bi - b_mean) for pi, bi in zip(p, b)) / (n - 1)
    variance = sum((bi - b_mean) ** 2 for bi in b) / (n - 1)
    if variance <= 0:
        return None
    return covariance / variance


def max_drawdown(values: Sequence[float | None]) -> tuple[float | None, float | None]:
    """Returns ``(max_drawdown, current_drawdown)`` as negative fractions."""
    peak = None
    worst = None
    current = None
    for value in values:
        if value is None or value <= 0:
            continue
        peak = value if peak is None else max(peak, value)
        drawdown = (value - peak) / peak
        worst = drawdown if worst is None else min(worst, drawdown)
        current = drawdown
    return worst, current


def historical_var(returns: Sequence[float], confidence: float = 0.95) -> float | None:
    """Historical VaR — the loss exceeded only (1-confidence) of the time."""
    if len(returns) < 20:
        return None
    ordered = sorted(returns)
    index = int((1.0 - confidence) * len(ordered))
    return ordered[max(0, min(len(ordered) - 1, index))]


def correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 20:
        return None
    x, y = list(a[-n:]), list(b[-n:])
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    vx = sum((xi - mx) ** 2 for xi in x)
    vy = sum((yi - my) ** 2 for yi in y)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


# ---------------------------------------------------------------------------
# Risk report
# ---------------------------------------------------------------------------
def analyze_risk(
    session: Session,
    portfolio: Portfolio,
    *,
    as_of: date | None = None,
    lookback_days: int = 252,
    config: dict[str, Any] | None = None,
) -> RiskReport:
    """Full portfolio risk assessment with limit-breach warnings."""
    from backend.portfolio.paper_trading import mark_to_market

    as_of = as_of or date.today()
    risk_cfg = config or load_yaml_config("risk")
    limits = risk_cfg.get("limits", {})
    speculative_strategies = set(
        (risk_cfg.get("strategy_classification", {}) or {}).get("speculative", [])
    )

    state = mark_to_market(session, portfolio, as_of=as_of)
    total_value = state["total_value"]
    positions: list[Position] = [p for p in state["positions"] if p.market_value is not None]

    report = RiskReport(
        as_of=as_of,
        portfolio_value=total_value,
        cash_weight=state["cash_weight"],
        gross_exposure=safe_or_none(safe_div(state["gross_exposure"], total_value)),
        net_exposure=safe_or_none(safe_div(state["net_exposure"], total_value)),
        position_count=len(positions),
    )
    if state["unpriced_tickers"]:
        report.unavailable.append(
            f"No current price for: {', '.join(state['unpriced_tickers'])}. "
            "These positions are excluded from every exposure measure below."
        )

    # --- Concentration -------------------------------------------------------
    weights: dict[str, float] = {}
    sector_weights: dict[str, float] = {}
    strategy_weights: dict[str, float] = {}
    for position in positions:
        weight = abs(position.market_value) / total_value if total_value > 0 else 0.0
        weights[position.ticker] = weight
        sector = position.sector or "Unclassified"
        sector_weights[sector] = sector_weights.get(sector, 0.0) + weight
        strategy = position.strategy or "unclassified"
        strategy_weights[strategy] = strategy_weights.get(strategy, 0.0) + weight

    report.sector_weights = {k: round(v, 4) for k, v in sorted(
        sector_weights.items(), key=lambda kv: -kv[1])}
    report.strategy_weights = {k: round(v, 4) for k, v in sorted(
        strategy_weights.items(), key=lambda kv: -kv[1])}
    if weights:
        ticker, weight = max(weights.items(), key=lambda kv: kv[1])
        report.largest_position = (ticker, round(weight, 4))
        # Herfindahl index: 1.0 = everything in one name.
        report.concentration_hhi = round(sum(w**2 for w in weights.values()), 4)

    # --- Return-based measures ----------------------------------------------
    snapshots = list(session.execute(
        select(PortfolioSnapshot)
        .where(
            PortfolioSnapshot.portfolio_id == portfolio.portfolio_id,
            PortfolioSnapshot.as_of <= as_of,
            PortfolioSnapshot.as_of >= as_of - timedelta(days=lookback_days * 2),
        )
        .order_by(PortfolioSnapshot.as_of)
    ).scalars().all())

    if len(snapshots) >= 20:
        values = [s.total_value for s in snapshots]
        portfolio_returns = returns_from_series(values)
        report.portfolio_volatility = annualized_volatility(portfolio_returns)
        report.max_drawdown, report.current_drawdown = max_drawdown(values)
        report.value_at_risk_95 = historical_var(portfolio_returns)

        benchmark_values = [s.benchmark_value for s in snapshots]
        if sum(1 for v in benchmark_values if v is not None) >= 20:
            report.beta = compute_beta(portfolio_returns, returns_from_series(benchmark_values))
        else:
            report.unavailable.append(
                "Beta unavailable: insufficient benchmark history stored alongside "
                "portfolio snapshots."
            )
    else:
        report.unavailable.append(
            f"Volatility, drawdown, beta and VaR need at least 20 daily snapshots; "
            f"{len(snapshots)} are stored. Run the daily snapshot job to build history."
        )

    # --- Liquidity risk ------------------------------------------------------
    for position in positions:
        turnover = _average_turnover(session, position.ticker, as_of=as_of)
        if turnover is None or turnover <= 0:
            continue
        days_to_exit = abs(position.market_value) / turnover
        if days_to_exit > 3.0:
            report.illiquid_positions.append((position.ticker, round(days_to_exit, 1)))
    report.illiquid_positions.sort(key=lambda x: -x[1])

    # --- Correlation clusters ------------------------------------------------
    report.correlation_clusters = _correlation_clusters(
        session, [p.ticker for p in positions], as_of=as_of
    )

    # --- Warnings ------------------------------------------------------------
    report.warnings = _build_warnings(
        report, limits, speculative_strategies, strategy_weights
    )
    for warning in report.warnings:
        if warning.severity in ("warning", "critical"):
            log_event(
                logger, EVENT_ALERT, warning.title,
                code=warning.code, severity=warning.severity,
                current=warning.current, limit=warning.limit,
            )
    return report


def safe_or_none(value: Any) -> float | None:
    return float(value) if is_available(value) else None


def _average_turnover(
    session: Session, ticker: str, *, as_of: date, days: int = 20
) -> float | None:
    rows = session.execute(
        select(PriceBar)
        .where(PriceBar.ticker == ticker.upper(), PriceBar.timestamp <= as_of)
        .order_by(PriceBar.timestamp.desc())
        .limit(days)
    ).scalars().all()
    values = [
        (r.close or r.adjusted_close) * r.volume
        for r in rows
        if (r.close or r.adjusted_close) and r.volume
    ]
    return sum(values) / len(values) if values else None


def _correlation_clusters(
    session: Session, tickers: Sequence[str], *, as_of: date, threshold: float = 0.7
) -> list[list[str]]:
    """Group holdings whose returns move together above *threshold*."""
    if len(tickers) < 2:
        return []
    series: dict[str, list[float]] = {}
    for ticker in tickers:
        rows = session.execute(
            select(PriceBar)
            .where(PriceBar.ticker == ticker.upper(), PriceBar.timestamp <= as_of)
            .order_by(PriceBar.timestamp.desc())
            .limit(120)
        ).scalars().all()
        closes = [r.adjusted_close or r.close for r in reversed(rows)]
        returns = returns_from_series(closes)
        if len(returns) >= 20:
            series[ticker] = returns

    clusters: list[list[str]] = []
    assigned: set[str] = set()
    names = list(series)
    for i, a in enumerate(names):
        if a in assigned:
            continue
        cluster = [a]
        for b in names[i + 1:]:
            if b in assigned:
                continue
            corr = correlation(series[a], series[b])
            if corr is not None and corr >= threshold:
                cluster.append(b)
                assigned.add(b)
        if len(cluster) > 1:
            assigned.add(a)
            clusters.append(cluster)
    return clusters


def _build_warnings(
    report: RiskReport,
    limits: dict[str, Any],
    speculative_strategies: set[str],
    strategy_weights: dict[str, float],
) -> list[RiskWarning]:
    warnings: list[RiskWarning] = []

    max_sector = float(limits.get("max_sector_weight", 0.30))
    for sector, weight in report.sector_weights.items():
        if weight > max_sector:
            warnings.append(RiskWarning(
                code="SECTOR_CONCENTRATION",
                severity="critical" if weight > max_sector * 1.25 else "warning",
                title="RISK ALERT — Sector concentration",
                message=(
                    f"{sector} sector exposure = {weight:.0%}\n\n"
                    f"Maximum allowed = {max_sector:.0%}\n\n"
                    f"Consider reducing exposure by {(weight - max_sector) * report.portfolio_value:,.0f} "
                    f"{'EGP' } to return within the limit."
                ),
                current=round(weight, 4), limit=max_sector,
            ))

    max_position = float(limits.get("max_position_weight", 0.20))
    if report.largest_position and report.largest_position[1] > max_position:
        ticker, weight = report.largest_position
        warnings.append(RiskWarning(
            code="POSITION_CONCENTRATION",
            severity="critical" if weight > max_position * 1.25 else "warning",
            title="RISK ALERT — Position concentration",
            message=(
                f"{ticker} is {weight:.0%} of the portfolio\n\n"
                f"Maximum allowed = {max_position:.0%}\n\nConsider trimming the position."
            ),
            current=round(weight, 4), limit=max_position, ticker=ticker,
        ))

    min_cash = float(limits.get("min_cash_weight", 0.05))
    if report.cash_weight is not None and report.cash_weight < min_cash:
        warnings.append(RiskWarning(
            code="CASH_FLOOR",
            severity="warning",
            title="RISK ALERT — Cash below minimum",
            message=(
                f"Cash = {report.cash_weight:.1%}\n\nMinimum required = {min_cash:.0%}\n\n"
                "The portfolio has no buffer for opportunities or redemptions."
            ),
            current=round(report.cash_weight, 4), limit=min_cash,
        ))

    max_speculative = float(limits.get("max_speculative_weight", 0.15))
    speculative_weight = sum(
        w for s, w in strategy_weights.items() if s in speculative_strategies
    )
    if speculative_weight > max_speculative:
        warnings.append(RiskWarning(
            code="SPECULATIVE_EXPOSURE",
            severity="warning",
            title="RISK ALERT — Speculative exposure",
            message=(
                f"Speculative strategies (swing, special situations, paper shorts) "
                f"= {speculative_weight:.0%}\n\nMaximum allowed = {max_speculative:.0%}"
            ),
            current=round(speculative_weight, 4), limit=max_speculative,
        ))

    max_dd = float(limits.get("max_portfolio_drawdown", 0.20))
    if report.current_drawdown is not None and abs(report.current_drawdown) > max_dd:
        warnings.append(RiskWarning(
            code="DRAWDOWN",
            severity="critical",
            title="RISK ALERT — Drawdown limit breached",
            message=(
                f"Current drawdown = {abs(report.current_drawdown):.1%}\n\n"
                f"Maximum tolerated = {max_dd:.0%}\n\n"
                "Review every open thesis before adding risk."
            ),
            current=round(abs(report.current_drawdown), 4), limit=max_dd,
        ))

    max_positions = int(limits.get("max_positions", 20))
    if report.position_count > max_positions:
        warnings.append(RiskWarning(
            code="POSITION_COUNT", severity="info",
            title="Position count above target",
            message=(
                f"{report.position_count} positions held against a target of "
                f"{max_positions}. Monitoring quality may suffer."
            ),
            current=report.position_count, limit=max_positions,
        ))

    if report.illiquid_positions:
        worst_ticker, days = report.illiquid_positions[0]
        warnings.append(RiskWarning(
            code="LIQUIDITY", severity="warning" if days > 10 else "info",
            title="Liquidity risk",
            message=(
                f"{worst_ticker} would take approximately {days:.1f} trading days to "
                "exit at 100% of average daily turnover"
                + (f", along with {len(report.illiquid_positions) - 1} other position(s)."
                   if len(report.illiquid_positions) > 1 else ".")
            ),
            current=days, ticker=worst_ticker,
        ))

    for cluster in report.correlation_clusters:
        warnings.append(RiskWarning(
            code="CORRELATION", severity="info",
            title="Correlated holdings",
            message=(
                f"{', '.join(cluster)} have moved together (correlation ≥ 0.7 over the "
                "last 120 sessions). They are unlikely to diversify one another."
            ),
        ))

    return warnings
