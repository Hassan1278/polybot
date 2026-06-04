from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from polybot.db import Base


class Market(Base):
    __tablename__ = "markets"

    market_id: Mapped[str] = mapped_column(String(80), primary_key=True)      # condition id
    slug: Mapped[str] = mapped_column(String(256), index=True)
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    liquidity_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    volume_24h_usdc: Mapped[float] = mapped_column(Float, default=0.0)
    yes_token_id: Mapped[str | None] = mapped_column(String(80))
    no_token_id: Mapped[str | None] = mapped_column(String(80))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
