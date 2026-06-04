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
    paper_starting_usdc: float = 10_000.0

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

    # db / cache
    database_url: str
    redis_url: str = "redis://redis:6379/0"

    # api
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    admin_token: SecretStr = Field(default=SecretStr("change_me"))

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
