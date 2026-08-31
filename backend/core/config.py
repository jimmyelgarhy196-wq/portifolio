"""Central configuration for GMG Investment Intelligence.

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

# --- Brand -----------------------------------------------------------------
BRAND_COMPANY = "GMG AI Solutions"
BRAND_PRODUCT = "GMG Investment Intelligence"
BRAND_TAGLINE = "Investment Intelligence for the Egyptian Market"
BRAND_SHORT = "GMG"

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

    # --- application / security -----------------------------------------------
    #: Base URL used in verification and reset links sent by email.
    base_url: str = "http://127.0.0.1:8000"
    #: Signing key for sessions and one-time tokens. MUST be set in production.
    auth_secret: str = ""
    session_cookie: str = "gmg_session"
    session_days: int = 14
    #: Set true behind HTTPS so the session cookie is never sent in clear.
    cookie_secure: bool = False
    #: Argon2 parameters. Defaults follow the RFC 9106 low-memory profile.
    argon2_time_cost: int = 3
    argon2_memory_kib: int = 65536
    argon2_parallelism: int = 4
    #: Failed logins allowed per (email, IP) inside the window before lockout.
    login_max_attempts: int = 8
    login_window_minutes: int = 15
    login_lockout_minutes: int = 20
    #: Generic API rate limit, requests per minute per client.
    rate_limit_per_minute: int = 120
    require_email_verification: bool = True
    admin_email: str = ""

    # --- subscription / billing -----------------------------------------------
    plan_code: str = "gmg_investment_intelligence"
    plan_name: str = "GMG Investment Intelligence"
    plan_price_egp: float = 300.0
    plan_interval: str = "month"
    trial_days: int = 7
    #: manual | none  — "manual" records intent and waits for an admin/gateway
    #: to confirm; there is no simulated card processing anywhere in this system.
    payment_provider: str = "manual"
    payment_api_key: str = ""
    payment_webhook_secret: str = ""

    # --- email ------------------------------------------------------------------
    #: console | smtp | none
    email_provider: str = "console"
    email_api_key: str = ""
    email_from: str = "no-reply@gmg-ai.example"
    email_from_name: str = "GMG Investment Intelligence"

    # --- market data ------------------------------------------------------------
    market_data_api_key: str = ""
    #: Minutes a quote may be delayed by the provider; surfaced in the UI.
    quote_delay_minutes: int = 15
    market_open_time: str = "10:00"
    market_close_time: str = "14:30"
    #: EGX trades Sunday-Thursday. 0=Mon .. 6=Sun.
    market_trading_days: str = "6,0,1,2,3"

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

    @property
    def signing_key(self) -> str:
        """Key for sessions and one-time tokens.

        In production this must come from EGX_AUTH_SECRET. A development
        fallback is derived so the app runs out of the box, but it is stable
        only for this working directory and is refused in production.
        """
        if self.auth_secret:
            return self.auth_secret
        if self.env.lower() in ("production", "prod"):
            raise RuntimeError(
                "EGX_AUTH_SECRET must be set in production. Sessions and email "
                "tokens are signed with it; without one every session is forgeable."
            )
        import hashlib

        return hashlib.sha256(f"gmg-dev-{PROJECT_ROOT}".encode()).hexdigest()

    @property
    def trading_weekdays(self) -> set[int]:
        out: set[int] = set()
        for part in str(self.market_trading_days).split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
        return out or {6, 0, 1, 2, 3}

    @property
    def payments_enabled(self) -> bool:
        """Whether a real payment gateway is wired up.

        False means checkout records intent only — no card is charged anywhere
        in this codebase, and the UI says so.
        """
        return self.payment_provider not in ("", "none", "manual")


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
