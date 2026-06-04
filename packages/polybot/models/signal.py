from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from polybot.db import Base


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    market_id: Mapped[str] = mapped_column(String(80), index=True)
    outcome: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    wallet_count: Mapped[int] = mapped_column(Integer)
    wallets: Mapped[list] = mapped_column(JSON)               # ["0x...", ...]
    avg_win_rate: Mapped[float] = mapped_column(Float)
    correlation_score: Mapped[float] = mapped_column(Float)
    target_price: Mapped[float] = mapped_column(Float)
    target_size_usdc: Mapped[float] = mapped_column(Float)
    gate_results: Mapped[dict] = mapped_column(JSON)          # {gate_name: {"pass": bool, "reason": str}}
    gate_pass: Mapped[bool] = mapped_column(Boolean, index=True)
    executed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
