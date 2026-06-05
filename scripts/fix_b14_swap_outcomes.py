"""One-shot: B14 retroactive fix — swap outcome labels on misrecorded fills/positions.

The pre-fix executor (`row[0] if outcome=="YES" else row[1]`) always bought the
NO-side token for any non-YES/NO outcome. That means:

- For outcome=outcomes[0] (FIRST outcome, e.g. "SANCHEZ" in [SANCHEZ, LAUTARO]):
  executor bought no_token_id = LAUTARO's token. The position recorded outcome=
  SANCHEZ but we actually hold LAUTARO tokens.

- For outcome=outcomes[1] (SECOND outcome, e.g. "LAUTARO"):
  executor bought no_token_id = LAUTARO's token. Position label and reality
  both say LAUTARO. CORRECT by coincidence.

- For "YES" / "NO" (binary): executor used the matching token. CORRECT.

This script:
1. Identifies all paper fills + positions where the recorded outcome matches
   outcomes[0] of the market (= "first outcome was intended, but we actually
   bought second outcome's token").
2. Swaps the outcome label to outcomes[1] in both fills and positions.
3. If the swap creates a duplicate position row (because we ALSO have a
   correctly-recorded second-outcome position), merges them: weighted-avg
   the avg_price by size, sum the size and realized_pnl.
4. Idempotent via Redis sentinel.

Usage:
    docker compose exec api python -m scripts.fix_b14_swap_outcomes
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, select, update

from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Fill, Market, Position
from polybot.redis_bus import client as redis_client

log = get_logger(__name__)

SENTINEL_KEY = "polybot:backfill:b14_swap_outcomes_v2"  # bumped: v1 had case mismatch


def _first_outcome(outcomes_raw: Any) -> str | None:
    """outcomes[0] from a JSONB column (already a list when read by SA)."""
    if isinstance(outcomes_raw, list) and outcomes_raw:
        return str(outcomes_raw[0]).strip()
    if isinstance(outcomes_raw, str):
        try:
            d = json.loads(outcomes_raw)
            if isinstance(d, list) and d:
                return str(d[0]).strip()
        except json.JSONDecodeError:
            pass
    return None


def _second_outcome(outcomes_raw: Any) -> str | None:
    if isinstance(outcomes_raw, list) and len(outcomes_raw) > 1:
        return str(outcomes_raw[1]).strip()
    if isinstance(outcomes_raw, str):
        try:
            d = json.loads(outcomes_raw)
            if isinstance(d, list) and len(d) > 1:
                return str(d[1]).strip()
        except json.JSONDecodeError:
            pass
    return None


async def main() -> None:
    r = redis_client()
    if await r.get(SENTINEL_KEY):
        print("fix_b14_swap_outcomes: already applied (sentinel present). nothing to do.")
        return

    async with session_scope() as s:
        # Find every market with outcomes[] populated where ANY paper fill
        # recorded the first outcome (= those are misrecorded).
        rows = (await s.execute(
            select(Market.market_id, Market.outcomes)
            .where(Market.outcomes.isnot(None))
        )).all()
        markets_by_id = {mid: outs for mid, outs in rows}

        # Get all paper fills that reference one of these markets
        fills = (await s.execute(
            select(Fill).where(Fill.mode == "paper")
        )).scalars().all()

        # Collect work: per fill, decide if it needs swap
        swap_fills: list[tuple[int, str, str]] = []  # (fill_id, old_outcome, new_outcome)
        for f in fills:
            outs = markets_by_id.get(f.market_id)
            if outs is None:
                continue
            first = _first_outcome(outs)
            second = _second_outcome(outs)
            if not first or not second:
                continue
            # The fill's outcome matches the FIRST outcome (case-insensitive,
            # whitespace-trimmed) → it was misrecorded. Swap to second.
            if (f.outcome or "").strip().upper() == first.upper() \
                    and (f.outcome or "").strip().upper() != "YES" \
                    and (f.outcome or "").strip().upper() != "NO":
                # Normalise to UPPERCASE — that's the canonical form used
                # by the signal generator. Without normalisation the swap
                # creates duplicate positions ("LYNN VISION" uppercase from
                # original + "Lynn Vision" titlecase from swap).
                swap_fills.append((f.id, f.outcome, second.upper()))

        if not swap_fills:
            print("fix_b14_swap_outcomes: no misrecorded fills found.")
            await r.set(SENTINEL_KEY, "1")
            return

        print(f"fix_b14_swap_outcomes: {len(swap_fills)} fills to swap")

        # Apply fill swaps
        for fid, old_oc, new_oc in swap_fills:
            await s.execute(
                update(Fill).where(Fill.id == fid).values(outcome=new_oc)
            )

        # Rebuild ALL paper positions from fills — safest path because we
        # need to merge swapped-into rows with already-correctly-recorded
        # second-outcome rows.
        all_fills_now = (await s.execute(
            select(Fill).where(Fill.mode == "paper").order_by(Fill.ts)
        )).scalars().all()

        per_pos: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: {"shares": 0.0, "cost": 0.0, "realized": 0.0}
        )
        for f in all_fills_now:
            key = (f.market_id, f.outcome or "")
            sh = float(f.size_shares or 0.0)
            pr = float(f.price or 0.0)
            fee = float(f.fee_usdc or 0.0)
            if f.side == "BUY" and f.status == "filled":
                per_pos[key]["shares"] += sh
                per_pos[key]["cost"] += sh * pr
                per_pos[key]["realized"] -= fee   # paper.py books fee as realized
            elif f.side in ("SELL", "SETTLE") and f.status in ("filled", "settled"):
                # SELL/SETTLE: reduce shares, add realized = (price - avg) * shares - fee
                old = per_pos[key]
                if old["shares"] > 1e-9:
                    avg_now = old["cost"] / old["shares"]
                else:
                    avg_now = pr
                old["realized"] += (pr - avg_now) * sh - fee
                old["shares"] -= sh
                # cost basis shrinks proportionally
                if old["shares"] > 1e-9:
                    old["cost"] = old["shares"] * avg_now
                else:
                    old["cost"] = 0.0

        # Wipe + rewrite all PAPER positions for clean state
        await s.execute(
            delete(Position).where(Position.wallet == "PAPER")
        )

        new_positions = 0
        for (mid, oc), agg in per_pos.items():
            shares = round(agg["shares"], 8)
            cost = agg["cost"]
            avg_price = cost / agg["shares"] if agg["shares"] > 1e-9 else 0.0
            s.add(Position(
                wallet="PAPER",
                market_id=mid,
                outcome=oc,
                size_shares=shares,
                avg_price=avg_price,
                realized_pnl_usdc=round(agg["realized"], 8),
            ))
            new_positions += 1

    await r.set(SENTINEL_KEY, "1")
    print(f"✓ fix_b14_swap_outcomes: swapped {len(swap_fills)} fills, rebuilt {new_positions} positions, sentinel set.")


if __name__ == "__main__":
    asyncio.run(main())
