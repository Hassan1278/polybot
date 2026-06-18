"""One-shot: backfill `markets.category` for rows the old narrow tag-matcher
left NULL.

Re-classifies every NULL-category market by keyword on its stored question +
slug (tags aren't persisted on the row). Only fills NULLs — never overwrites an
existing bucket, so correctly-classified sports/other markets are untouched.

Concurrency-safe: reads first, then writes in small batches grouped by
category, each its own short transaction with a deadlock retry — so it can run
alongside the live `market_ingest` job (which also writes `markets` every 5 min)
without the two livelocking each other. Idempotent (re-running only touches
still-NULL rows).

Usage (from host):
    docker compose -f docker-compose.yml -f docker-compose.prod.yml \
        exec signals python -m scripts.backfill_categories
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from polybot.categorize import classify_keywords
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market

log = get_logger(__name__)

BATCH = 200          # rows per UPDATE — small txns release locks fast
MAX_RETRY = 6        # deadlock victims just retry; ingest holds locks briefly


async def _apply(cat: str, ids: list[str]) -> int:
    """UPDATE one chunk to `cat`, retrying on deadlock. Returns rows changed.
    The `category IS NULL` guard keeps it idempotent and avoids clobbering a
    value the ingest job set in between."""
    for attempt in range(MAX_RETRY):
        try:
            async with session_scope() as s:
                res = await s.execute(
                    update(Market)
                    .where(Market.market_id.in_(ids), Market.category.is_(None))
                    .values(category=cat)
                )
            return res.rowcount or 0
        except OperationalError as exc:
            if "deadlock" in str(exc).lower() and attempt < MAX_RETRY - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise
    return 0


async def main() -> None:
    # 1) Read NULL-category markets (short read txn; ids sorted so our write
    #    lock order is deterministic and matches ingest's by-PK ordering).
    async with session_scope() as s:
        rows = (await s.execute(
            select(Market.market_id, Market.question, Market.slug)
            .where(Market.category.is_(None))
            .order_by(Market.market_id)
        )).all()

    # 2) Classify in Python (no DB locks held).
    by_cat: dict[str, list[str]] = defaultdict(list)
    for mid, question, slug in rows:
        cat = classify_keywords(question, slug)
        if cat:
            by_cat[cat].append(mid)

    # 3) Apply in small, retryable batches.
    updated: dict[str, int] = {}
    for cat, ids in by_cat.items():
        for i in range(0, len(ids), BATCH):
            n = await _apply(cat, ids[i:i + BATCH])
            updated[cat] = updated.get(cat, 0) + n

    total = sum(updated.values())
    log.info("backfill_categories_done", scanned=len(rows), updated=total, by_category=updated)
    print(f"scanned {len(rows)} NULL-category markets; classified {total}: {dict(updated)}")


if __name__ == "__main__":
    asyncio.run(main())
