from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
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
    # outcomes is the *ordered* outcome name list for this market.
    # outcomes[i] corresponds to clobTokenIds[i] on Polymarket — i.e.
    # outcomes[0] ↔ yes_token_id, outcomes[1] ↔ no_token_id. For YES/NO
    # markets it's ["Yes", "No"]; for sports it's e.g. ["TYLOO", "Lynn Vision"];
    # for multi-candidate markets it can be longer than 2.
    # See packages/polybot/market_resolver.py:token_for_outcome().
    outcomes: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
