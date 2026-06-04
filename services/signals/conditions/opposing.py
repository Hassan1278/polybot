"""Soft gate: if other top wallets are on the OPPOSITE side recently, downgrade."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select

from polybot.models import Trade, Wallet
from services.signals.conditions.base import GateContext, GateResult


class OpposingSmartMoney:
    name = "opposing_smart_money"
    type = "soft"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.penalty = float(params.get("penalty", 0.25))
        self.window_min = int(params.get("check_window_minutes", 60))

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")
        mid = ctx.candidate["market_id"]
        my_side = ctx.candidate["side"].upper()
        opp = "SELL" if my_side == "BUY" else "BUY"
        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=self.window_min)

        n = (await ctx.session.execute(
            select(func.count(func.distinct(Trade.wallet)))
            .join(Wallet, Wallet.address == Trade.wallet)
            .where(and_(
                Wallet.is_active.is_(True),
                Trade.market_id == mid,
                Trade.side == opp,
                Trade.ts >= cutoff,
            ))
        )).scalar_one()

        if n > 0:
            return GateResult(self.name, self.type, True, f"opposing_count={n}",
                              score_adjust=-self.penalty * n)
        return GateResult(self.name, self.type, True, "no_opposition")
