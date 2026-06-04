"""Pull active markets from Gamma and upsert into `markets`."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.clients import GammaClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market

log = get_logger(__name__)


def _category_from_tags(tags: list[str], mapping: dict[str, list[str]]) -> str | None:
    flat: dict[str, str] = {t: cat for cat, ts in mapping.items() for t in ts}
    for t in tags:
        if t in flat:
            return flat[t]
    return None


async def run_market_ingest() -> None:
    from polybot.yaml_config import categories_cfg
    cats = categories_cfg.get().get("categories", {})
    tag_map = {cat: c["tags"] for cat, c in cats.items() if c.get("enabled")}

    g = GammaClient()
    try:
        page_size = 100  # Gamma caps each page at 100 regardless of `limit`
        page = 0
        total = 0
        while True:
            ms = await g.markets(limit=page_size, offset=page * page_size,
                                 active=True, closed=False)
            if not ms:
                break
            async with session_scope() as s:
                for m in ms:
                    if not m.get("conditionId"):
                        continue
                    # Tags may live on the market itself OR on its parent event(s).
                    raw_tags: list[dict] = list(m.get("tags") or [])
                    for ev in (m.get("events") or []):
                        raw_tags.extend(ev.get("tags") or [])
                    tags = [str(t.get("slug", "")).lower() for t in raw_tags if t]
                    cat = _category_from_tags(tags, tag_map)

                    # clobTokenIds comes back as a JSON-encoded string: '["yes_id", "no_id"]'
                    raw_tokens = m.get("clobTokenIds")
                    tokens: list[str] = []
                    if isinstance(raw_tokens, str) and raw_tokens.startswith("["):
                        try:
                            tokens = [str(t) for t in json.loads(raw_tokens)]
                        except json.JSONDecodeError:
                            tokens = []
                    elif isinstance(raw_tokens, list):
                        tokens = [str(t) for t in raw_tokens]
                    yes_tid = tokens[0] if len(tokens) > 0 else None
                    no_tid  = tokens[1] if len(tokens) > 1 else None

                    end_dt = None
                    if m.get("endDate"):
                        try:
                            end_dt = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
                        except (ValueError, TypeError):
                            end_dt = None

                    stmt = pg_insert(Market).values(
                        market_id=m["conditionId"],
                        slug=m.get("slug") or "",
                        question=m.get("question") or "",
                        category=cat,
                        end_date=end_dt,
                        resolved=bool(m.get("closed")),
                        outcome=m.get("outcome"),
                        liquidity_usdc=float(m.get("liquidity") or 0),
                        volume_24h_usdc=float(m.get("volume24hr") or 0),
                        yes_token_id=yes_tid,
                        no_token_id=no_tid,
                        updated_at=datetime.now(tz=timezone.utc),
                    ).on_conflict_do_update(
                        index_elements=["market_id"],
                        set_={"liquidity_usdc": pg_insert(Market).excluded.liquidity_usdc,
                              "volume_24h_usdc": pg_insert(Market).excluded.volume_24h_usdc,
                              "category": pg_insert(Market).excluded.category,
                              "resolved": pg_insert(Market).excluded.resolved,
                              "outcome": pg_insert(Market).excluded.outcome,
                              "updated_at": pg_insert(Market).excluded.updated_at},
                    )
                    await s.execute(stmt)
                    total += 1
            if len(ms) < page_size:
                break
            page += 1
            if page > 50:    # safety: 5 000 markets is plenty
                break
        log.info("market_ingest_done", total=total)
    finally:
        await g.close()
