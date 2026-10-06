"""Runtime configuration. Every limit the agent obeys lives here, loaded from env / .env."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    # --- Deriv ---
    deriv_app_id: str = ""
    deriv_pat: SecretStr = SecretStr("")
    deriv_account_id: str = ""
    deriv_rest_url: str = "https://api.derivws.com"
    # Hosts the WebSocket may connect to. The path must still be /ws/demo (see deriv.guard).
    deriv_ws_allowed_hosts: frozenset[str] = frozenset({"api.derivws.com"})

    # --- Decision models ---
    typesafe_api_key: SecretStr = SecretStr("")
    typesafe_model: str = "jev-1.13.0"
    cf_account_id: str = ""
    cf_api_token: SecretStr = SecretStr("")
    cf_model: str = "@cf/cloudflare/clef-flash"
    anthropic_api_key: SecretStr = SecretStr("")

    # --- Telegram ---
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    # --- Storage ---
    database_url: str = "postgresql://agent:agent@localhost:5432/agent"

    # --- Market ---
    symbol: str = "1HZ100V"
    buffer_ticks: int = Field(600, ge=120)
    min_history_ticks: int = Field(120, ge=10)
    stale_tick_s: float = Field(5.0, gt=0, le=60)

    # --- Connection ---
    ws_ping_interval_s: float = Field(30.0, gt=0)
    ws_silence_timeout_s: float = Field(15.0, gt=0)
    ws_backoff_max_s: float = Field(30.0, gt=0)

    # --- Trading (fixed by code, never by the model) ---
    decision_interval_s: int = Field(60, ge=10)
    duration_ticks: int = Field(5, ge=1, le=10)
    stake_usd: Decimal = Field(Decimal("1.00"), gt=0)
    max_stake_usd: Decimal = Field(Decimal("2.00"), gt=0)
    max_open_positions: int = Field(1, ge=1)
    daily_loss_cap_usd: Decimal = Field(Decimal("20.00"), gt=0)
    cooldown_after_losses: int = Field(3, ge=1)
    cooldown_minutes: int = Field(10, ge=1)

    # --- Model-output thresholds ---
    min_confidence: float = Field(0.55, ge=0.5, lt=1.0)
    approval_band_high: float = Field(0.65, ge=0.5, le=1.0)
    approval_timeout_s: int = Field(90, ge=10)

    # --- Spend caps per provider per UTC day (USD) ---
    spend_cap_jev_usd: Decimal = Decimal("0.50")
    spend_cap_clef_usd: Decimal = Decimal("0.50")
    spend_cap_anthropic_usd: Decimal = Decimal("1.00")

    # --- Safety ---
    kill_switch: bool = False

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.stake_usd > self.max_stake_usd:
            raise ValueError("stake_usd must not exceed max_stake_usd")
        if self.approval_band_high < self.min_confidence:
            raise ValueError("approval_band_high must be >= min_confidence")
        if self.min_history_ticks > self.buffer_ticks:
            raise ValueError("min_history_ticks must be <= buffer_ticks")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
