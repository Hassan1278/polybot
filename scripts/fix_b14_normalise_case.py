"""One-shot: normalise outcome casing on fills + positions to canonical UPPER.

After fix_b14_swap_outcomes.py ran v1, swapped fills got titlecase from
outcomes[] (e.g. "Lynn Vision") while existing fills had uppercase from the
signal generator (e.g. "LYNN VISION"). That leaves the same (market, outcome)
split across two casing variants — duplicate positions, broken lookups.

This script:
1. UPDATES every paper fill to outcome = UPPER(outcome).
2. DELETEs all paper positions, rebuilds from fills (so the merge is correct).

Idempotent: re-running is a no-op once all fills are already uppercase.

Usage:
    docker compose exec api python -m scripts.fix_b14_normalise_case
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from sqlalchemy import delete, select, update

from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Fill, Position

log = get_logger(__name__)


async def main() -> None:
    async with session_scope() as s:
        fills = (await s.execute(
            select(Fill).where(Fill.mode == "paper")
        )).scalars().all()

        normalised = 0
        for f in fills:
            up = (f.outcome or "").upper()
            if up != f.outcome:
                await s.execute(
                    update(Fill).where(Fill.id == f.id).values(outcome=up)
                )
                normalised += 1

        print(f"normalised {normalised} fill outcomes to UPPER")

        # Rebuild paper positions from fills (post-normalisation)
        all_fills = (await s.execute(
            select(Fill).where(Fill.mode == "paper").order_by(Fill.ts)
        )).scalars().all()

        per_pos: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: {"shares": 0.0, "cost": 0.0, "realized": 0.0}
        )
        for f in all_fills:
            key = (f.market_id, (f.outcome or "").upper())
            sh = float(f.size_shares or 0.0)
            pr = float(f.price or 0.0)
            fee = float(f.fee_usdc or 0.0)
            if f.side == "BUY" and f.status == "filled":
                per_pos[key]["shares"] += sh
                per_pos[key]["cost"] += sh * pr
                per_pos[key]["realized"] -= fee
            elif f.side in ("SELL", "SETTLE") and f.status in ("filled", "settled"):
                old = per_pos[key]
                if old["shares"] > 1e-9:
                    avg_now = old["cost"] / old["shares"]
                else:
                    avg_now = pr
                old["realized"] += (pr - avg_now) * sh - fee
                old["shares"] -= sh
                if old["shares"] > 1e-9:
                    old["cost"] = old["shares"] * avg_now
                else:
                    old["cost"] = 0.0

        await s.execute(delete(Position).where(Position.wallet == "PAPER"))

        new_n = 0
        for (mid, oc), agg in per_pos.items():
            s.add(Position(
                wallet="PAPER", market_id=mid, outcome=oc,
                size_shares=round(agg["shares"], 8),
                avg_price=(agg["cost"] / agg["shares"]) if agg["shares"] > 1e-9 else 0.0,
                realized_pnl_usdc=round(agg["realized"], 8),
            ))
            new_n += 1

    print(f"rebuilt {new_n} merged positions")


if __name__ == "__main__":
    asyncio.run(main())
