"""Just-in-time market resolution.

When a trade or signal references a market we haven't bulk-ingested, we look
it up on Gamma, classify it against our category config, and upsert it. After
that the regular gate chain works.

Cached in Redis for an hour to avoid hammering Gamma during clustering bursts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.clients import GammaClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market
from polybot.redis_bus import client as redis_client
from polybot.yaml_config import categories_cfg

log = get_logger(__name__)

CACHE_KEY = "polybot:resolved_market:{mid}"
CACHE_TTL = 3600  # 1 h


def _category_from_tags(tags: list[str]) -> str | None:
    cats = categories_cfg.get().get("categories", {})
    flat: dict[str, str] = {}
    for cat, c in cats.items():
        if not c.get("enabled"):
            continue
        for t in c.get("tags") or []:
            flat[str(t).lower()] = cat
    for t in tags:
        cat = flat.get(str(t).lower())
        if cat:
            return cat
    return None


async def ensure_market(market_id: str) -> Market | None:
    """Return the Market row for `market_id`, fetching from Gamma if missing."""
    async with session_scope() as s:
        existing = (await s.execute(
            select(Market).where(Market.market_id == market_id)
        )).scalar_one_or_none()
    if existing:
        return existing

    r = redis_client()
    if await r.get(CACHE_KEY.format(mid=market_id)) == "miss":
        return None

    g = GammaClient()
    try:
        m = await g.market_by_condition_id(market_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("market_resolve_failed", market=market_id, err=str(exc))
        m = None
    finally:
        await g.close()

    if not m:
        await r.set(CACHE_KEY.format(mid=market_id), "miss", ex=CACHE_TTL)
        return None

    # extract tags from market AND its parent events
    raw_tags: list[dict] = list(m.get("tags") or [])
    for ev in (m.get("events") or []):
        raw_tags.extend(ev.get("tags") or [])
    tags = [str(t.get("slug", "")).lower() for t in raw_tags if t]
    cat = _category_from_tags(tags)

    raw_tokens = m.get("clobTokenIds")
    tokens: list[str] = []
    if isinstance(raw_tokens, str) and raw_tokens.startswith("["):
        try:
            tokens = [str(t) for t in json.loads(raw_tokens)]
        except json.JSONDecodeError:
            tokens = []
    elif isinstance(raw_tokens, list):
        tokens = [str(t) for t in raw_tokens]

    end_dt = None
    if m.get("endDate"):
        try:
            end_dt = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            end_dt = None

    async with session_scope() as s:
        await s.execute(pg_insert(Market).values(
            market_id=m["conditionId"],
            slug=m.get("slug") or "",
            question=m.get("question") or "",
            category=cat,
            end_date=end_dt,
            resolved=bool(m.get("closed")),
            outcome=m.get("outcome"),
            liquidity_usdc=float(m.get("liquidity") or 0),
            volume_24h_usdc=float(m.get("volume24hr") or 0),
            yes_token_id=tokens[0] if len(tokens) > 0 else None,
            no_token_id=tokens[1] if len(tokens) > 1 else None,
            updated_at=datetime.now(tz=timezone.utc),
        ).on_conflict_do_nothing(index_elements=["market_id"]))
        out = (await s.execute(
            select(Market).where(Market.market_id == market_id)
        )).scalar_one_or_none()

    log.info("market_resolved_jit", market=market_id, category=cat,
             question=(m.get("question") or "")[:60])
    return out
