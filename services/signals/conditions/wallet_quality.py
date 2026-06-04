"""Wallet-quality gate.

NOTE on the Sharpe metric: our `_daily_sharpe` is computed from
`SUM(SELL_notional) - SUM(BUY_notional) - fees` per day. A wallet that's
*accumulating* a position (more BUYs than SELLs over a 30-day window) will
register negative cash flow every day → negative mean → **negative Sharpe**.

That's a CASH-FLOW direction, NOT a skill signal. Filtering on it would
reject exactly the wallets we want to mirror: the ones building conviction
positions ahead of a catalyst. So this gate now filters on:

1. Realised win-rate (the genuine skill signal — already computed from
   Polymarket's `/positions` realizedPnl, not trade prints).
2. A LOOSE Sharpe sanity bound that only kicks in for extreme outliers
   (configurable via `max_negative_sharpe`, default disabled = -inf).

We also flag the cluster's average win-rate and Sharpe into `ctx.extra`
so downstream gates / position sizing can use them as soft inputs.
"""

from __future__ import annotations

from sqlalchemy import and_, select

from polybot.models import WalletStats
from services.signals.conditions.base import GateContext, GateResult


class WalletQuality:
    name = "wallet_quality"
    type = "hard"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.min_avg_win_rate = float(params.get("min_avg_win_rate", 0.50))
        # `max_negative_sharpe` is a *floor*: signals are rejected only if
        # the cluster's avg Sharpe is BELOW this value. Defaults to -inf so
        # the Sharpe filter is effectively off — Sharpe is a cash-flow
        # artefact for accumulating wallets and not a useful gate metric.
        # Set e.g. to -3.0 to filter only the truly anomalous outliers.
        self.max_negative_sharpe = float(params.get("max_negative_sharpe", float("-inf")))
        self.window = params.get("window", "30d")

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")

        wallets: list[str] = ctx.candidate["wallets"]
        if not wallets:
            return GateResult(self.name, self.type, False, "no_wallets")

        rows = (await ctx.session.execute(
            select(WalletStats.win_rate, WalletStats.sharpe)
            .where(and_(
                WalletStats.address.in_(wallets),
                WalletStats.window == self.window,
            ))
        )).all()

        # win_rate / sharpe may be NULL for wallets without enough realised
        # data. Average over whatever we have, separately per metric.
        wr_vals = [float(r[0]) for r in rows if r[0] is not None]
        sh_vals = [float(r[1]) for r in rows if r[1] is not None]

        # No stats at all — be permissive. Downstream gates still filter.
        if not wr_vals and not sh_vals:
            ctx.extra["avg_win_rate"] = None
            ctx.extra["avg_sharpe"] = None
            return GateResult(self.name, self.type, True, "no_stats — permissive pass")

        wr = sum(wr_vals) / len(wr_vals) if wr_vals else None
        sh = sum(sh_vals) / len(sh_vals) if sh_vals else None
        ctx.extra["avg_win_rate"] = wr
        ctx.extra["avg_sharpe"] = sh

        # Win-rate floor — the only metric that genuinely measures skill.
        if wr is not None and wr < self.min_avg_win_rate:
            return GateResult(
                self.name, self.type, False,
                f"avg_wr={wr:.3f}<{self.min_avg_win_rate}",
            )

        # Extreme Sharpe outlier guard (off by default). Use this only if
        # you've seen a cluster of clearly broken wallets in the audit log.
        if sh is not None and sh < self.max_negative_sharpe:
            return GateResult(
                self.name, self.type, False,
                f"avg_sharpe={sh:.3f}<{self.max_negative_sharpe}",
            )

        parts: list[str] = []
        if wr is not None:
            parts.append(f"wr={wr:.3f}")
        if sh is not None:
            parts.append(f"sharpe={sh:.3f}")
        return GateResult(self.name, self.type, True, ", ".join(parts) or "ok")
