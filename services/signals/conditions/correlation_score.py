from __future__ import annotations

from services.signals.conditions.base import GateContext, GateResult


class CorrelationScore:
    name = "correlation_score"
    type = "hard"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.min_score = float(params.get("min_score", 0.65))
        self.min_wallets = int(params.get("min_wallets", 3))
        # Optional per-category score floors that OVERRIDE min_score for the
        # listed categories. Use it to demand higher conviction on noisier
        # buckets (e.g. sports). Anything not listed uses min_score.
        raw = params.get("category_min_score") or {}
        self.category_min_score = {str(k).lower(): float(v) for k, v in raw.items()}

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")
        n = len(ctx.candidate.get("wallets") or [])
        score = float(ctx.candidate.get("correlation_score", 0.0))
        if n < self.min_wallets:
            return GateResult(self.name, self.type, False, f"n={n}<{self.min_wallets}")
        # category_match (gate 1) populated ctx.extra["category"]; apply its
        # per-category floor if one is configured, else the global min_score.
        cat = str(ctx.extra.get("category") or "").lower()
        threshold = self.category_min_score.get(cat, self.min_score)
        if score < threshold:
            return GateResult(self.name, self.type, False,
                              f"score={score:.3f}<{threshold:.3f}[{cat or 'default'}]")
        return GateResult(self.name, self.type, True,
                          f"n={n}, score={score:.3f}>={threshold:.3f}")
