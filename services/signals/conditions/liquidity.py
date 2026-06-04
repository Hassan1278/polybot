"""Walk the orderbook to confirm we can fill at acceptable slip."""

from __future__ import annotations

from sqlalchemy import select

from polybot.clients import ClobClient
from polybot.models import Market
from services.signals.conditions.base import GateContext, GateResult


class Liquidity:
    name = "liquidity"
    type = "hard"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        self.min_depth = float(params.get("min_book_depth_usdc", 500.0))
        self.max_slip_pct = float(params.get("max_slippage_pct", 2.0))

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")
        mid = ctx.candidate["market_id"]
        outcome = ctx.candidate.get("outcome", "YES").upper()
        side = ctx.candidate["side"].upper()
        target_usdc = float(ctx.extra.get("target_size_usdc", 25.0))

        row = (await ctx.session.execute(
            select(Market.yes_token_id, Market.no_token_id).where(Market.market_id == mid)
        )).first()
        if not row:
            return GateResult(self.name, self.type, False, "market_unknown")
        token_id = row[0] if outcome == "YES" else row[1]
        if not token_id:
            return GateResult(self.name, self.type, False, "no_token_id")

        c = ClobClient()
        try:
            book = await c.book(token_id)
        finally:
            await c.close()
        if not book:
            return GateResult(self.name, self.type, False, "book_empty")

        side_levels = book.get("asks") if side == "BUY" else book.get("bids")
        if not side_levels:
            return GateResult(self.name, self.type, False, "no_side_levels")
        # levels: [{"price": "0.32", "size": "120.5"}, ...]; for BUY we sort asc, for SELL desc
        levels = sorted(side_levels, key=lambda l: float(l["price"]), reverse=(side == "SELL"))
        best = float(levels[0]["price"])

        depth_usdc = 0.0
        notional = 0.0
        shares_filled = 0.0
        for lv in levels:
            p = float(lv["price"]); sz = float(lv["size"])
            depth_usdc += p * sz
            if notional + p * sz >= target_usdc:
                need_sz = (target_usdc - notional) / p
                shares_filled += need_sz
                notional = target_usdc
                last_price = p
                break
            shares_filled += sz
            notional += p * sz
            last_price = p
        else:
            return GateResult(self.name, self.type, False, f"insufficient_depth: {notional:.0f}<{target_usdc}")

        slip_pct = abs(last_price - best) / best * 100.0
        ctx.extra["expected_avg_price"] = notional / shares_filled if shares_filled else best
        ctx.extra["book_depth_usdc"] = depth_usdc
        ctx.extra["best_price"] = best
        if depth_usdc < self.min_depth:
            return GateResult(self.name, self.type, False, f"depth={depth_usdc:.0f}<{self.min_depth}")
        if slip_pct > self.max_slip_pct:
            return GateResult(self.name, self.type, False, f"slip={slip_pct:.2f}%>{self.max_slip_pct}")
        return GateResult(self.name, self.type, True, f"depth={depth_usdc:.0f}, slip={slip_pct:.2f}%")
