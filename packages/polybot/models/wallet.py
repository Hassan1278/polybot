from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from polybot.db import Base


class Wallet(Base):
    __tablename__ = "wallets"

    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_active: Mapped[bool] = mapped_column(default=True, index=True)


class WalletStats(Base):
    """Rolling stats per (wallet, window). Recomputed by the signals service.

    `win_rate` and `sharpe` are nullable: NULL means "not enough realised data
    to compute honestly" (< 5 decisions / < 5 trading days), never a
    misleading 0 or 1.
    """

    __tablename__ = "wallet_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(42), index=True)
    window: Mapped[str] = mapped_column(String(16))     # "7d", "30d", "90d", "all"
    pnl_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_trade_size: Mapped[float] = mapped_column(Float, default=0.0)
    n_decisions: Mapped[int] = mapped_column(Integer, default=0)
    n_open_positions: Mapped[int] = mapped_column(Integer, default=0)
    n_total_positions: Mapped[int] = mapped_column(Integer, default=0)
    n_trade_days: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_wallet_stats_addr_window", "address", "window"),)
