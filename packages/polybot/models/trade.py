from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from polybot.db import Base


class Trade(Base):
    """Hypertable. Migration converts to TimescaleDB hypertable on `ts`."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str | None] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    wallet: Mapped[str] = mapped_column(String(42), index=True)
    market_id: Mapped[str] = mapped_column(String(80), index=True)
    outcome: Mapped[str] = mapped_column(String(8))                  # YES | NO
    side: Mapped[str] = mapped_column(String(8))                     # BUY | SELL
    size_shares: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)                      # probability
    notional_usdc: Mapped[float] = mapped_column(Float)
    fee_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(16))                  # gamma | subgraph | ws | onchain

    __table_args__ = (
        Index("ix_trades_wallet_ts", "wallet", "ts"),
        Index("ix_trades_market_ts", "market_id", "ts"),
    )
