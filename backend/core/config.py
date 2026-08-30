"""Central configuration for EGX ALPHA.

All configuration arrives from three layers, later layers overriding earlier:

1. Defaults declared here.
2. ``config/*.yaml`` files (weights, risk limits, universe).
3. Environment variables / ``.env`` (see ``.env.example``).

Nothing in this module reads a secret from anywhere but the environment.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DATABASE_DIR = PROJECT_ROOT / "database"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DATA_DIR = PROJECT_ROOT / "data"


def _split_csv(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


class Settings(BaseSettings):
    """Environment-driven settings. Field names map to ``EGX_``-prefixed vars."""

    model_config = SettingsConfigDict(
        env_prefix="EGX_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- core -----------------------------------------------------------------
    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    log_format: str = "text"

    # --- database -------------------------------------------------------------
    database_url: str = f"sqlite:///{DATABASE_DIR / 'egx_alpha.db'}"

    # --- providers ------------------------------------------------------------
    market_data_providers: list[str] = Field(default_factory=lambda: ["csv", "yahoo"])
    fundamental_providers: list[str] = Field(default_factory=lambda: ["csv"])
    news_providers: list[str] = Field(default_factory=lambda: ["rss"])
    disclosure_providers: list[str] = Field(default_factory=lambda: ["egx"])
    csv_data_dir: str = "data/manual"
    allow_synthetic_data: bool = False

    yahoo_base_url: str = "https://query1.finance.yahoo.com"
    disclosure_base_url: str = "https://www.egx.com.eg"
    aggregator_api_key: str = ""
    aggregator_base_url: str = ""
    refinitiv_api_key: str = ""
    bloomberg_api_key: str = ""

    http_timeout_seconds: float = 20.0
    http_max_retries: int = 3
    http_rate_limit_per_second: float = 4.0

    # --- AI -------------------------------------------------------------------
    ai_model: str = "claude-sonnet-5"
    ai_max_tokens: int = 4000
    ai_temperature: float = 0.2
    ai_max_calls_per_run: int = 60

    # --- portfolio ------------------------------------------------------------
    portfolio_capital: float = 1_000_000.0
    portfolio_currency: str = "EGP"
    benchmark_ticker: str = "EGX30"
    max_position_weight: float = 0.20
    max_sector_weight: float = 0.30
    min_cash_weight: float = 0.05
    max_speculative_weight: float = 0.15
    max_risk_per_position: float = 0.02
    max_portfolio_drawdown: float = 0.20
    commission_bps: float = 20.0
    slippage_bps: float = 15.0

    # --- scheduling -----------------------------------------------------------
    scheduler_enabled: bool = False
    weekly_cron: str = "0 18 * * 5"
    timezone: str = "Africa/Cairo"

    # --- notifications --------------------------------------------------------
    notifications_enabled: bool = False
    notify_email_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notify_webhook_url: str = ""

    # --- safety ---------------------------------------------------------------
    live_trading_enabled: bool = False

    @field_validator(
        "market_data_providers",
        "fundamental_providers",
        "news_providers",
        "disclosure_providers",
        mode="before",
    )
    @classmethod
    def _parse_provider_list(cls, v: Any) -> list[str]:
        return _split_csv(v)

    # -- derived ---------------------------------------------------------------
    @property
    def anthropic_api_key(self) -> str:
        """Read unprefixed so the standard ``ANTHROPIC_API_KEY`` works."""
        return os.environ.get("ANTHROPIC_API_KEY", "").strip()

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def csv_dir(self) -> Path:
        p = Path(self.csv_data_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# YAML configuration (weights, limits, universe)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8)
def load_yaml_config(name: str) -> dict[str, Any]:
    """Load ``config/<name>.yaml``. Returns ``{}`` when the file is absent."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def reload_configs() -> None:
    """Drop cached YAML + settings so edits take effect without a restart."""
    load_yaml_config.cache_clear()
    get_settings.cache_clear()
