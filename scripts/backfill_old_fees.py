"""One-shot: subtract un-accounted BUY fees from old positions' realized_pnl.

The original paper.py (pre-Workflow refactor) recorded BUY fills with
`fee_usdc` set, but did NOT deduct that fee from the position's
`realized_pnl_usdc` ledger. The newer paper.py does. As a result, positions
opened before the refactor (notably the May 29 Hormuz $25 and NIP $25
positions, each with $0.50 fee) over-report their realized PnL by the
fee amount.

This script is idempotent via a Redis sentinel — re-running is a no-op.

Usage:
    docker compose exec api python -m scripts.backfill_old_fees
    # then to undo (if you ever need to):
    docker compose exec redis redis-cli DEL polybot:backfill:old_fees_v1
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Fill, Position
from polybot.redis_bus import client as redis_client

log = get_logger(__name__)

SENTINEL_KEY = "polybot:backfill:old_fees_v1"

# Cutoff: fills before this timestamp are considered "old format" and
# their fees weren't booked to realized_pnl. After this, paper.py was
# updated and started booking fees correctly via realized_delta=-fee.
CUTOFF = datetime(2026, 6, 4, 10, 0, 0, tzinfo=timezone.utc)


async def main() -> None:
    r = redis_client()
    if await r.get(SENTINEL_KEY):
        print("backfill_old_fees: already applied (sentinel present). nothing to do.")
        return

    async with session_scope() as s:
        # Find old paper fills that DO have a fee
        old_fills = (
            await s.execute(
                select(Fill)
                .where(Fill.mode == "paper")
                .where(Fill.ts < CUTOFF)
                .where(Fill.fee_usdc > 0)
            )
        ).scalars().all()

        if not old_fills:
            print("backfill_old_fees: no old fee-bearing fills found.")
            await r.set(SENTINEL_KEY, "1")
            return

        # Group fee by (market_id, outcome) — that's the position key
        from collections import defaultdict
        fee_by_pos: dict[tuple[str, str], float] = defaultdict(float)
        for f in old_fills:
            fee_by_pos[(f.market_id, f.outcome)] += float(f.fee_usdc or 0.0)

        print(f"backfill_old_fees: {len(old_fills)} fills affecting {len(fee_by_pos)} positions")

        updated = 0
        for (mid, oc), fee_total in fee_by_pos.items():
            pos = (await s.execute(
                select(Position).where(
                    Position.market_id == mid,
                    Position.outcome == oc,
                    Position.wallet == "PAPER",
                )
            )).scalar_one_or_none()
            if not pos:
                continue
            old_realized = float(pos.realized_pnl_usdc or 0.0)
            new_realized = old_realized - fee_total
            pos.realized_pnl_usdc = new_realized
            print(f"  {mid[:14]} {oc[:24]:24s} realized {old_realized:+.4f} -> {new_realized:+.4f}  (-${fee_total:.4f})")
            updated += 1

    await r.set(SENTINEL_KEY, "1")
    print(f"✓ backfill_old_fees: {updated} positions updated. Sentinel set.")


if __name__ == "__main__":
    asyncio.run(main())
