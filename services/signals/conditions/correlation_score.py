from __future__ import annotations

from services.signals.conditions.base import GateContext, GateResult


class CorrelationScore:
    name = "correlation_score"
    type = "hard"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.min_score = float(params.get("min_score", 0.65))
        self.min_wallets = int(params.get("min_wallets", 3))

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")
        n = len(ctx.candidate.get("wallets") or [])
        score = float(ctx.candidate.get("correlation_score", 0.0))
        if n < self.min_wallets:
            return GateResult(self.name, self.type, False, f"n={n}<{self.min_wallets}")
        if score < self.min_score:
            return GateResult(self.name, self.type, False, f"score={score:.3f}<{self.min_score}")
        return GateResult(self.name, self.type, True, f"n={n}, score={score:.3f}")
