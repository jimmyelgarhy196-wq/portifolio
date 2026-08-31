"""Evaluation and delivery of user-created alerts.

An alert only fires on data that actually exists. Where a quote or an indicator
is unavailable, the alert is skipped and the reason recorded — it is never
treated as a value of zero, which would fire every "price below" alert on the
platform at once.

Demonstration data never fires an alert. Sending someone an email saying their
stock crossed a level, on the basis of a generated price, would be the single
most damaging thing this system could do.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analytics.indicators import rsi, sma
from backend.core.logging_config import get_logger
from backend.data.models import PriceBar
from backend.data.saas_models import Quote, User, UserAlert
from backend.market.quotes import get_quotes
from backend.notify.email_service import send_email

logger = get_logger(__name__)

CONDITIONS: dict[str, str] = {
    "price_above": "Price rises above",
    "price_below": "Price falls below",
    "pct_move_up": "Daily gain exceeds",
    "pct_move_down": "Daily fall exceeds",
    "rsi_above": "RSI (14) rises above",
    "rsi_below": "RSI (14) falls below",
    "high_52w": "Price reaches a 52-week high",
    "low_52w": "Price reaches a 52-week low",
    "ma_cross_up": "Price crosses above its 50-day average",
    "ma_cross_down": "Price crosses below its 50-day average",
}

#: Conditions that need a threshold from the user.
NEEDS_THRESHOLD = {
    "price_above", "price_below", "pct_move_up", "pct_move_down",
    "rsi_above", "rsi_below",
}


@dataclass
class AlertEvaluation:
    alert: UserAlert
    triggered: bool
    message: str | None = None
    skipped_reason: str | None = None


def _indicator(session: Session, ticker: str) -> dict[str, float | None]:
    bars = list(session.execute(
        select(PriceBar).where(PriceBar.ticker == ticker, PriceBar.close.isnot(None))
        .order_by(PriceBar.timestamp.desc()).limit(260)
    ).scalars().all())
    closes = [float(b.close) for b in reversed(bars)]
    if len(closes) < 2:
        return {"rsi": None, "sma50": None, "prev_close": None, "prev_sma50": None}
    rsi_series = rsi(closes, 14)
    sma_series = sma(closes, 50)
    return {
        "rsi": rsi_series[-1] if rsi_series else None,
        "sma50": sma_series[-1] if sma_series else None,
        "prev_close": closes[-2],
        "prev_sma50": sma_series[-2] if len(sma_series) > 1 else None,
    }


def evaluate_alert(session: Session, alert: UserAlert, quote: Quote | None) -> AlertEvaluation:
    """Decide whether one alert should fire right now."""
    if not alert.active:
        return AlertEvaluation(alert, False, skipped_reason="Alert is paused.")
    if quote is None or quote.price is None:
        return AlertEvaluation(
            alert, False,
            skipped_reason=f"N/A — no quote is available for {alert.ticker}.")
    if quote.is_demo:
        return AlertEvaluation(
            alert, False,
            skipped_reason=(
                "Skipped: the only price available is demonstration data. GMG does not "
                "fire alerts on generated prices."))

    price = float(quote.price)
    condition = alert.condition
    threshold = alert.threshold

    if condition in NEEDS_THRESHOLD and threshold is None:
        return AlertEvaluation(alert, False, skipped_reason="No threshold is set.")

    if condition == "price_above" and price > threshold:
        return AlertEvaluation(alert, True, f"{alert.ticker} traded at {price:,.2f}, above your {threshold:,.2f} level.")
    if condition == "price_below" and price < threshold:
        return AlertEvaluation(alert, True, f"{alert.ticker} traded at {price:,.2f}, below your {threshold:,.2f} level.")

    if condition in {"pct_move_up", "pct_move_down"}:
        if quote.change_pct is None:
            return AlertEvaluation(alert, False, skipped_reason="Daily change is unavailable.")
        move = float(quote.change_pct)
        if condition == "pct_move_up" and move >= threshold:
            return AlertEvaluation(alert, True, f"{alert.ticker} is up {move:.2%} today.")
        if condition == "pct_move_down" and move <= -abs(threshold):
            return AlertEvaluation(alert, True, f"{alert.ticker} is down {move:.2%} today.")
        return AlertEvaluation(alert, False)

    if condition == "high_52w":
        if quote.week52_high is None:
            return AlertEvaluation(alert, False, skipped_reason="No 52-week high is stored.")
        if price >= float(quote.week52_high):
            return AlertEvaluation(alert, True, f"{alert.ticker} reached a 52-week high at {price:,.2f}.")
        return AlertEvaluation(alert, False)

    if condition == "low_52w":
        if quote.week52_low is None:
            return AlertEvaluation(alert, False, skipped_reason="No 52-week low is stored.")
        if price <= float(quote.week52_low):
            return AlertEvaluation(alert, True, f"{alert.ticker} reached a 52-week low at {price:,.2f}.")
        return AlertEvaluation(alert, False)

    indicators = _indicator(session, alert.ticker)

    if condition in {"rsi_above", "rsi_below"}:
        value = indicators["rsi"]
        if value is None:
            return AlertEvaluation(alert, False,
                                   skipped_reason="Not enough price history to compute RSI.")
        if condition == "rsi_above" and value > threshold:
            return AlertEvaluation(alert, True, f"{alert.ticker} RSI(14) is {value:.1f}, above {threshold:.0f}.")
        if condition == "rsi_below" and value < threshold:
            return AlertEvaluation(alert, True, f"{alert.ticker} RSI(14) is {value:.1f}, below {threshold:.0f}.")
        return AlertEvaluation(alert, False)

    if condition in {"ma_cross_up", "ma_cross_down"}:
        sma50 = indicators["sma50"]
        prev_close = indicators["prev_close"]
        prev_sma = indicators["prev_sma50"]
        if None in (sma50, prev_close, prev_sma):
            return AlertEvaluation(
                alert, False,
                skipped_reason="Not enough price history for a 50-day moving average.")
        crossed_up = prev_close <= prev_sma and price > sma50
        crossed_down = prev_close >= prev_sma and price < sma50
        if condition == "ma_cross_up" and crossed_up:
            return AlertEvaluation(alert, True, f"{alert.ticker} crossed above its 50-day average ({sma50:,.2f}).")
        if condition == "ma_cross_down" and crossed_down:
            return AlertEvaluation(alert, True, f"{alert.ticker} crossed below its 50-day average ({sma50:,.2f}).")
        return AlertEvaluation(alert, False)

    return AlertEvaluation(alert, False, skipped_reason=f"Unknown condition '{condition}'.")


def run_user_alerts(session: Session, *, send: bool = True) -> list[AlertEvaluation]:
    """Evaluate every active alert and email the ones that fired."""
    alerts = list(session.execute(
        select(UserAlert).where(UserAlert.active.is_(True))
    ).scalars().all())
    if not alerts:
        return []

    quotes = get_quotes(session, sorted({a.ticker for a in alerts}))
    results: list[AlertEvaluation] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for alert in alerts:
        result = evaluate_alert(session, alert, quotes.get(alert.ticker))
        results.append(result)
        if not result.triggered:
            continue
        alert.last_triggered_at = now
        alert.trigger_count = (alert.trigger_count or 0) + 1
        alert.last_message = result.message
        if send and alert.email_delivery:
            user = session.get(User, alert.user_id)
            if user is not None:
                send_email(session, to=user.email, template="alert", name=user.display_name,
                    ticker=alert.ticker, message=result.message,
                    condition=CONDITIONS.get(alert.condition, alert.condition),
                )
    session.flush()
    logger.info(
        "User alerts evaluated: %d checked, %d triggered",
        len(results), sum(1 for r in results if r.triggered),
    )
    return results
