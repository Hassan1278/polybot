from __future__ import annotations

from sqlalchemy import and_, select

from polybot.models import WalletStats
from services.signals.conditions.base import GateContext, GateResult


class WalletQuality:
    name = "wallet_quality"
    type = "hard"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.min_avg_win_rate = float(params.get("min_avg_win_rate", 0.55))
        self.min_avg_sharpe = float(params.get("min_avg_sharpe", 0.0))
        self.window = params.get("window", "30d")

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")
        wallets: list[str] = ctx.candidate["wallets"]
        if not wallets:
            return GateResult(self.name, self.type, False, "no_wallets")
        rows = (await ctx.session.execute(
            select(WalletStats.win_rate, WalletStats.sharpe)
            .where(and_(WalletStats.address.in_(wallets), WalletStats.window == self.window))
        )).all()
        # win_rate / sharpe can be NULL for wallets without enough realised data.
        # Filter those out per metric before averaging — be tolerant: as long as
        # we have at least one wallet with each metric, decide on that subset.
        wr_vals = [float(r[0]) for r in rows if r[0] is not None]
        sh_vals = [float(r[1]) for r in rows if r[1] is not None]

        # If NOBODY in the cluster has a win-rate yet, we cannot judge quality.
        # Be permissive: pass the gate but flag in ctx so downstream gates can
        # use the absence as a signal too.
        if not wr_vals and not sh_vals:
            ctx.extra["avg_win_rate"] = None
            ctx.extra["avg_sharpe"] = None
            return GateResult(self.name, self.type, True, "no_stats — permissive pass")

        wr = sum(wr_vals) / len(wr_vals) if wr_vals else None
        sh = sum(sh_vals) / len(sh_vals) if sh_vals else None
        ctx.extra["avg_win_rate"] = wr
        ctx.extra["avg_sharpe"] = sh

        if wr is not None and wr < self.min_avg_win_rate:
            return GateResult(self.name, self.type, False,
                              f"avg_wr={wr:.3f}<{self.min_avg_win_rate}")
        if sh is not None and sh < self.min_avg_sharpe:
            return GateResult(self.name, self.type, False,
                              f"avg_sharpe={sh:.3f}<{self.min_avg_sharpe}")

        parts = []
        if wr is not None: parts.append(f"wr={wr:.3f}")
        if sh is not None: parts.append(f"sharpe={sh:.3f}")
        return GateResult(self.name, self.type, True, ", ".join(parts) or "ok")
