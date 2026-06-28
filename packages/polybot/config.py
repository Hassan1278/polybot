"""Single source of truth for env-driven configuration.

Read once at import-time. Anything that needs runtime mutation (kill-switch,
risk caps overridden from the dashboard) lives in Redis, not here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # mode
    trading_mode: Literal["paper", "live"] = "paper"
    paper_starting_usdc: float = 300.0

    # polymarket endpoints
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_data_url: str = "https://data-api.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws"

    # polygon
    polygon_rpc_url: str = "https://polygon-rpc.com"
    polygon_chain_id: int = 137

    # goldsky
    goldsky_subgraph_url: str = ""

    # signing
    polymarket_private_key: SecretStr | None = None
    polymarket_funder_address: str | None = None
    polymarket_signature_type: int = 1  # 0=EOA  1=email/magic  2=browser
    # AES-256-GCM master key for wallet_credentials table. Base64-encoded
    # 32 raw bytes. Required in live mode; paper mode can run without it
    # (uses polymarket_private_key env fallback when can_sign==True).
    # Generate: python -c "from polybot.crypto import generate_master_key; print(generate_master_key())"
    # Loss is unrecoverable — encrypted wallet rows become useless.
    wallet_encryption_key: SecretStr | None = None

    # db / cache
    database_url: str
    redis_url: str = "redis://redis:6379/0"

    # api
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # admin_token MUST be overridden in .env. The default "change_me" is
    # rejected at runtime in live-mode (see scripts/validate.py + the
    # require_admin dependency) so any prod deploy with the default token
    # cannot accept admin commands. In paper mode the default still works
    # to keep local dev frictionless.
    admin_token: SecretStr = Field(default=SecretStr("change_me"))
    # CORS — comma-separated list of allowed origins for the API.
    # Empty/unset = only http://localhost:3000 (dev dashboard). Wildcard
    # ("*") is intentionally NOT supported here — would defeat credentials.
    cors_origins: str = "http://localhost:3000"

    # risk
    max_position_usdc: float = 25.0
    max_daily_loss_usdc: float = 50.0
    max_open_positions: int = 5
    cooldown_seconds_per_market: int = 300

    # wallet tracking
    top_wallets_per_category: int = 30
    min_win_rate: float = 0.55
    min_trade_count_30d: int = 20
    correlation_window_minutes: int = 15
    correlation_min_wallets: int = 3
    correlation_min_score: float = 0.65
    # Slow-category correlation: crypto/politics/macro/weather agree over HOURS,
    # not 15-min bursts, so they get a wider window + slower time-decay. Sports
    # keeps the tight `correlation_window_minutes` above (live-game bursts).
    # Without this, slow categories never reach min_wallets inside 15 min and
    # never fire — which is exactly why crypto produced zero signals.
    correlation_window_minutes_slow: int = 240
    correlation_half_life_seconds_slow: float = 5400.0  # 90-min decay

    # integrations
    sentry_dsn: str | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def can_sign(self) -> bool:
        return self.polymarket_private_key is not None and self.polymarket_funder_address is not None


settings = Settings()  # type: ignore[call-arg]
