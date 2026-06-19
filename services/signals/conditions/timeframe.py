from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from polybot.models import Market
from services.signals.conditions.base import GateContext, GateResult


class Timeframe:
    name = "timeframe"
    type = "hard"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.min_h = float(params.get("min_hours_to_resolve", 1))
        self.max_h = float(params.get("max_hours_to_resolve", 720))

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")
        row = (await ctx.session.execute(
            select(Market.end_date).where(Market.market_id == ctx.candidate["market_id"])
        )).first()
        if not row or row[0] is None:
            return GateResult(self.name, self.type, False, "no_end_date")
        end_date: datetime = row[0]
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        hours = (end_date - datetime.now(tz=timezone.utc)).total_seconds() / 3600
        ctx.extra["hours_to_resolve"] = hours
        if hours < self.min_h:
            return GateResult(self.name, self.type, False, f"h={hours:.1f}<{self.min_h}")
        if hours > self.max_h:
            return GateResult(self.name, self.type, False, f"h={hours:.1f}>{self.max_h}")
        return GateResult(self.name, self.type, True, f"h={hours:.1f}")
