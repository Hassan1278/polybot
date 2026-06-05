"""One-shot: backfill `markets.outcomes` for rows inserted before migration 0003.

Pulls each market with NULL outcomes from Gamma and saves the outcomes list.
Prioritises markets where we currently hold open positions (those affect live
mark-display today). Then fills the rest in batches with rate limiting.

Idempotent — re-running only touches still-NULL rows.

Usage (from host):
    docker compose exec api python -m scripts.backfill_market_outcomes
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select, update

from polybot.clients import GammaClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market, Position

log = get_logger(__name__)

CONCURRENCY = 4               # be polite to Gamma
PER_REQUEST_DELAY_S = 0.10


def _parse_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw.startswith("["):
        try:
            d = json.loads(raw)
            return [str(x) for x in d] if isinstance(d, list) else []
        except json.JSONDecodeError:
            return []
    return []


async def _market_ids_needing_outcomes(prefer_open: bool = True) -> list[str]:
    async with session_scope() as s:
        if prefer_open:
            # Markets we have open positions in — fix these FIRST so the
            # dashboard displays the right marks ASAP.
            rows = (await s.execute(
                select(Market.market_id)
                .join(Position, Position.market_id == Market.market_id)
                .where(Market.outcomes.is_(None))
                .where(Position.size_shares > 0)
                .distinct()
            )).all()
            urgent = [r[0] for r in rows]
        else:
            urgent = []

        rows = (await s.execute(
            select(Market.market_id).where(Market.outcomes.is_(None))
        )).all()
        all_missing = [r[0] for r in rows]

    # Deduped, urgent first
    seen: set[str] = set()
    ordered: list[str] = []
    for mid in urgent + all_missing:
        if mid in seen:
            continue
        seen.add(mid)
        ordered.append(mid)
    return ordered


async def _fetch_and_save(g: GammaClient, mid: str) -> bool:
    try:
        m = await g.market_by_condition_id(mid)
    except Exception as exc:  # noqa: BLE001
        log.warning("backfill_outcomes_fetch_failed", market=mid, err=str(exc))
        return False
    if not m:
        return False
    outcomes_list = _parse_json_list(m.get("outcomes"))
    if not outcomes_list:
        return False
    async with session_scope() as s:
        await s.execute(
            update(Market)
            .where(Market.market_id == mid)
            .values(outcomes=outcomes_list)
        )
    return True


async def main() -> None:
    ids = await _market_ids_needing_outcomes(prefer_open=True)
    log.info("backfill_outcomes_starting", total=len(ids))
    if not ids:
        log.info("backfill_outcomes_nothing_to_do")
        return

    g = GammaClient()
    sem = asyncio.Semaphore(CONCURRENCY)
    saved = 0
    failed = 0

    async def _one(mid: str) -> None:
        nonlocal saved, failed
        async with sem:
            ok = await _fetch_and_save(g, mid)
            if ok:
                saved += 1
            else:
                failed += 1
            await asyncio.sleep(PER_REQUEST_DELAY_S)

    try:
        await asyncio.gather(*[_one(mid) for mid in ids])
    finally:
        await g.close()

    log.info("backfill_outcomes_done", saved=saved, failed=failed, total=len(ids))
    print(f"\n✓ saved={saved}  failed={failed}  total_attempted={len(ids)}")


if __name__ == "__main__":
    asyncio.run(main())
