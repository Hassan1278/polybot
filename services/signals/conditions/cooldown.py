from __future__ import annotations

from services.signals.conditions.base import GateContext, GateResult

KEY = "polybot:cooldown:market:{mid}"


class Cooldown:
    name = "cooldown"
    type = "hard"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.cd_seconds = int(params.get("cooldown_minutes_per_market", 30)) * 60

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")
        k = KEY.format(mid=ctx.candidate["market_id"])
        last = await ctx.redis.get(k)
        if last is not None:
            return GateResult(self.name, self.type, False, f"cooldown_active:{last}")
        await ctx.redis.set(k, "1", ex=self.cd_seconds)
        return GateResult(self.name, self.type, True, f"cd_set:{self.cd_seconds}s")
