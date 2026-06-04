from __future__ import annotations

from sqlalchemy import select

from polybot.models import Market
from services.signals.conditions.base import GateContext, GateResult


class CategoryMatch:
    name = "category_match"
    type = "hard"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.allow = set(params.get("allow") or [])

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")
        mid = ctx.candidate["market_id"]
        row = (await ctx.session.execute(select(Market.category).where(Market.market_id == mid))).first()
        cat = row[0] if row else None
        ctx.extra["category"] = cat
        if not cat:
            return GateResult(self.name, self.type, False, "unknown_category")
        if self.allow and cat not in self.allow:
            return GateResult(self.name, self.type, False, f"category_not_allowed:{cat}")
        return GateResult(self.name, self.type, True, f"category:{cat}")
