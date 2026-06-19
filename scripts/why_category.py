"""why_category.py — show HOW a market got classified (which tag let it in).

Find markets by question substring (searched in our DB) or by market_id,
re-fetch their live Gamma tags, and print which of our category tags matched
(the leak path) plus what classify_market() now returns. Use it to see how an
off-topic market slipped into a tradable category.

Usage (on the VPS):
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec ingest \
      python -m scripts.why_category "Elon Musk post"
  docker compose ... exec ingest python -m scripts.why_category 0xMARKETID ...
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from polybot.categorize import classify_market
from polybot.clients import GammaClient
from polybot.db import session_scope
from polybot.market_resolver import _enabled_tag_map
from polybot.models import Market


async def main(args: list[str]) -> None:
    if not args:
        print('usage: why_category.py "<question substring>" | <market_id> ...')
        return

    mids: list[str] = []
    async with session_scope() as s:
        for a in args:
            if a.startswith("0x") and len(a) > 20:
                mids.append(a)
            else:
                rows = (await s.execute(
                    select(Market.market_id).where(Market.question.ilike(f"%{a}%")).limit(15)
                )).all()
                mids += [r[0] for r in rows]
    if not mids:
        print("no matching markets in DB")
        return

    tag_map = _enabled_tag_map()
    flat = {str(t).lower(): c for c, ts in tag_map.items() for t in (ts or [])}

    g = GammaClient()
    try:
        for mid in mids:
            try:
                gm = await g.market_by_condition_id(mid)
            except Exception as exc:  # noqa: BLE001
                print(f"{mid}: gamma error {exc}")
                continue
            if not gm:
                print(f"{mid}: not found on gamma")
                continue
            raw = list(gm.get("tags") or [])
            for ev in (gm.get("events") or []):
                raw += ev.get("tags") or []
            tags = [str(t.get("slug", "")).lower() for t in raw if t]
            matched = sorted({(t, flat[t]) for t in tags if t in flat})
            cat = classify_market(tags=tags, question=gm.get("question"),
                                  slug=gm.get("slug"), tag_map=tag_map)
            print(f"\n{(gm.get('question') or '')[:74]}")
            print(f"  classify_market -> {cat}")
            print(f"  tag matches (leak path): {matched or 'none'}")
            print(f"  all tags: {tags}")
    finally:
        await g.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
