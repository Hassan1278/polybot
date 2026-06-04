from __future__ import annotations

from services.signals.conditions.base import GateContext, GateResult


class RiskReward:
    name = "risk_reward"
    type = "hard"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.min_rr = float(params.get("min_rr", 1.3))
        self.max_entry = float(params.get("max_entry_price", 0.85))
        self.min_entry = float(params.get("min_entry_price", 0.05))

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")
        entry = float(ctx.extra.get("expected_avg_price") or ctx.candidate.get("avg_price", 0.0))
        if entry <= 0:
            return GateResult(self.name, self.type, False, "no_entry_price")
        if entry > self.max_entry:
            return GateResult(self.name, self.type, False, f"entry={entry:.3f}>{self.max_entry}")
        if entry < self.min_entry:
            return GateResult(self.name, self.type, False, f"entry={entry:.3f}<{self.min_entry}")

        # For a BUY on YES at probability p, upside = 1 - p, downside = p.
        # R:R = upside / downside.
        side = ctx.candidate["side"].upper()
        if side == "BUY":
            upside = 1.0 - entry
            downside = entry
        else:
            upside = entry
            downside = 1.0 - entry
        rr = upside / max(downside, 1e-6)
        ctx.extra["rr"] = rr
        if rr < self.min_rr:
            return GateResult(self.name, self.type, False, f"rr={rr:.2f}<{self.min_rr}")
        return GateResult(self.name, self.type, True, f"rr={rr:.2f}")
