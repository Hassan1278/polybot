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

        # Price band is direction-aware. A SELL at 0.95 has the same R:R
        # profile as a BUY at 0.05 (upside 0.95, downside 0.05). Without
        # mirroring the band, SELLs near the top of the book (very cheap
        # "no" outcome bets) were dropped despite excellent R:R.
        side = ctx.candidate["side"].upper()
        if side == "BUY":
            lo, hi = self.min_entry, self.max_entry
        else:
            lo, hi = 1.0 - self.max_entry, 1.0 - self.min_entry
        if entry > hi:
            return GateResult(self.name, self.type, False, f"entry={entry:.3f}>{hi:.3f}")
        if entry < lo:
            return GateResult(self.name, self.type, False, f"entry={entry:.3f}<{lo:.3f}")

        # For a BUY on YES at probability p, upside = 1 - p, downside = p.
        # R:R = upside / downside.
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
