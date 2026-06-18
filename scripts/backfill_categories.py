"""One-shot: backfill `markets.category` for rows the old narrow tag-matcher
left NULL.

Re-classifies every NULL-category market by keyword on its stored question +
slug (tags aren't persisted on the row). Only fills NULLs — never overwrites an
existing bucket, so correctly-classified sports/other markets are untouched.

Idempotent: re-running only touches still-NULL rows. Markets that still don't
match any of politics/crypto/macro stay NULL (correctly blocked by the gate).

Usage (from host):
    docker compose -f docker-compose.yml -f docker-compose.prod.yml \
        exec signals python -m scripts.backfill_categories
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from polybot.categorize import classify_keywords
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market

log = get_logger(__name__)


async def main() -> None:
    updated: dict[str, int] = {}
    scanned = 0
    async with session_scope() as s:
        rows = (await s.execute(
            select(Market).where(Market.category.is_(None))
        )).scalars().all()
        scanned = len(rows)
        for m in rows:
            cat = classify_keywords(m.question, m.slug)
            if cat:
                m.category = cat
                updated[cat] = updated.get(cat, 0) + 1
        # session_scope commits on exit.

    total = sum(updated.values())
    log.info("backfill_categories_done", scanned=scanned, updated=total, by_category=updated)
    print(f"scanned {scanned} NULL-category markets; classified {total}: {updated}")


if __name__ == "__main__":
    asyncio.run(main())
