from __future__ import annotations

from services.signals.conditions.base import GateContext, GateResult

KEY = "polybot:cooldown:market:{mid}"


class Cooldown:
    """READ-ONLY cooldown gate.

    Previously the gate SET the cooldown key inside evaluate(), which
    locked the market even if a later hard gate then failed the signal
    (the cooldown timer ran while no signal actually fired). It also
    fought with the soft opposing_smart_money gate that runs AFTER
    cooldown — a soft penalty could drop the score below threshold while
    the cooldown was already armed.

    Engine.process_candidate is now responsible for SETting the key in
    Redis only AFTER `gate_pass_hard` AND signal persistence — see
    engine.py's `arm_cooldown` call site. The gate just READs.
    """
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
        # Don't write here — engine arms the cooldown ONLY when the signal
        # actually fires. Stash the configured TTL so engine can read it
        # without duplicating the config lookup.
        ctx.extra["cooldown_seconds"] = self.cd_seconds
        return GateResult(self.name, self.type, True, f"cd_ttl:{self.cd_seconds}s")
