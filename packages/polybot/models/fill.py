from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from polybot.db import Base


class Fill(Base):
    __tablename__ = "fills"
    # Partial UNIQUE index `uq_fills_signal_id` on `signal_id WHERE signal_id
    # IS NOT NULL` is created in migration 0004 to enforce at-most-one-fill-
    # per-signal. NULL signal_ids (SETTLE rows from pnl_loop) are exempt
    # because they're not signal-driven. The executor also pre-checks for an
    # existing fill in `services/executor/main.py:handle` so the database
    # IntegrityError is a belt-and-braces safety net.

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    mode: Mapped[str] = mapped_column(String(8))            # paper | live
    market_id: Mapped[str] = mapped_column(String(80), index=True)
    outcome: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    size_shares: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    notional_usdc: Mapped[float] = mapped_column(Float)
    fee_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16))         # filled | partial | rejected
    venue_order_id: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(String(512))
