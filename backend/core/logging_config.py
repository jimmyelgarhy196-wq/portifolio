"""Structured logging for EGX ALPHA.

Two formats are supported: human-readable ``text`` for local work and ``json``
for production log shipping. Domain events (data updates, provider failures,
research runs, portfolio changes, report generation, backtests) are emitted
through :func:`log_event` so they carry consistent structured fields.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from backend.core.config import get_settings

_CONFIGURED = False

# Domain event categories used across the application.
EVENT_DATA_UPDATE = "data_update"
EVENT_PROVIDER_FAILURE = "provider_failure"
EVENT_RESEARCH_RUN = "research_run"
EVENT_PORTFOLIO_CHANGE = "portfolio_change"
EVENT_REPORT = "report"
EVENT_BACKTEST = "backtest"
EVENT_ALERT = "alert"
EVENT_ERROR = "error"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "event_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-28s %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = getattr(record, "event_fields", None)
        if extra:
            kv = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None)
            if kv:
                base = f"{base} | {kv}"
        return base


def configure_logging(force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if settings.log_format.lower() == "json" else TextFormatter()
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    # Third-party noise control.
    for noisy in ("httpx", "httpcore", "apscheduler", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    event: str,
    message: str,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a structured domain event."""
    logger.log(level, message, extra={"event_fields": {"event": event, **fields}})
