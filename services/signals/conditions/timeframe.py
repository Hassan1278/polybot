from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from polybot.models import Market
from services.signals.conditions.base import GateContext, GateResult


class Timeframe:
    name = "timeframe"
    type = "hard"

    # Edge categories trade multi-day theses, so a market resolving within the
    # slow floor there is intraday coinflip noise (daily BTC up/down, "above $X
    # today"). Sports/worldcup keep the low `min_hours_to_resolve` (live events).
    _SLOW_CATEGORIES = {"crypto", "politics", "macro", "weather"}

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.min_h = float(params.get("min_hours_to_resolve", 1))
        self.max_h = float(params.get("max_hours_to_resolve", 720))
        # Falls back to min_h if not configured.
        self.min_h_slow = float(params.get("min_hours_to_resolve_slow", self.min_h))

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
        # category_match (gate 1) set ctx.extra["category"]; edge categories get
        # the higher floor so daily coinflips are filtered out.
        cat = str(ctx.extra.get("category") or "").lower()
        min_floor = self.min_h_slow if cat in self._SLOW_CATEGORIES else self.min_h
        if hours < min_floor:
            return GateResult(self.name, self.type, False, f"h={hours:.1f}<{min_floor}[{cat or 'fast'}]")
        if hours > self.max_h:
            return GateResult(self.name, self.type, False, f"h={hours:.1f}>{self.max_h}")
        return GateResult(self.name, self.type, True, f"h={hours:.1f}")
