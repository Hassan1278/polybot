"""Just-in-time market resolution.

When a trade or signal references a market we haven't bulk-ingested, we look
it up on Gamma, classify it against our category config, and upsert it. After
that the regular gate chain works.

Also exposes `token_for_outcome(market, outcome_str)` — the SINGLE place in
the codebase that maps a position's outcome string to the correct CLOB
token_id. Previously this logic was duplicated (and wrong for non-YES/NO
outcomes) in services/api/routes/positions.py and services/executor/pnl_loop.py.

Cached in Redis for an hour to avoid hammering Gamma during clustering bursts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.clients import GammaClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market
from polybot.redis_bus import client as redis_client
from polybot.categorize import classify_market
from polybot.yaml_config import categories_cfg

log = get_logger(__name__)

CACHE_KEY = "polybot:resolved_market:{mid}"
CACHE_TTL = 3600  # 1 h


def _parse_json_list(raw: Any) -> list[str]:
    """Parse Gamma's '["a","b"]' string-or-list responses into a clean list."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw.startswith("["):
        try:
            data = json.loads(raw)
            return [str(x) for x in data] if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    return []


def event_id_from_gamma(m: dict[str, Any]) -> str | None:
    """Stable identifier for a market's parent event, for one-position-per-event
    grouping. Prefers the event's numeric id, falls back to slug then ticker.
    Returns None when the market has no parent event. Shared by the bulk ingest
    and this JIT resolver so the two write-paths can't drift."""
    for ev in (m.get("events") or []):
        if not isinstance(ev, dict):
            continue
        ident = ev.get("id") or ev.get("slug") or ev.get("ticker")
        if ident:
            return str(ident)[:80]
    return None


def token_for_outcome(market: Any, outcome: str | None) -> str | None:
    """Map (market, outcome_string) → the correct CLOB token_id.

    This is the canonical mapping used everywhere we need to query a mark
    price or settle a position. It handles three cases:

    1. **Legacy YES/NO**: outcome="YES" → yes_token_id, "NO" → no_token_id.
       Works for politics / crypto / macro markets.

    2. **Multi-outcome via outcomes column**: outcome="TYLOO" in a market
       with outcomes=["TYLOO", "Lynn Vision"] → idx=0 → yes_token_id.
       outcome="Lynn Vision" → idx=1 → no_token_id. This is the *correct*
       path for sport_other and any non-binary market.

    3. **Fallback (no outcomes data)**: returns yes_token_id, which is
       the legacy buggy behaviour, but is the best guess when outcomes
       JSON is missing (e.g. row was inserted before migration 0003).
       Logged at DEBUG so we can spot stale rows during ops.

    `market` is duck-typed: any object with attributes `yes_token_id`,
    `no_token_id`, `outcomes` works (DB model rows, dicts wrapped in
    SimpleNamespace, etc.). `outcome` can be None → returns None.
    """
    if outcome is None:
        return None
    upper = outcome.strip().upper()
    if not upper:
        return None

    yes_tid = getattr(market, "yes_token_id", None)
    no_tid = getattr(market, "no_token_id", None)

    if upper == "YES":
        return yes_tid
    if upper == "NO":
        return no_tid

    outcomes_raw = getattr(market, "outcomes", None)
    outcomes: list[str] = []
    if isinstance(outcomes_raw, list):
        outcomes = [str(o) for o in outcomes_raw]
    elif isinstance(outcomes_raw, str):
        outcomes = _parse_json_list(outcomes_raw)

    if outcomes:
        upper_outcomes = [o.strip().upper() for o in outcomes]
        try:
            idx = upper_outcomes.index(upper)
        except ValueError:
            idx = -1
        if idx == 0:
            return yes_tid
        if idx == 1:
            return no_tid
        # idx >= 2 (rare multi-candidate market) — we only persist 2 tokens,
        # so we can't price these without extending the schema. Return None
        # so callers can fall back to avg_price.
        if idx >= 2:
            return None

    # No outcomes data + non-binary outcome → legacy fallback, log it so
    # ops can see which markets are stale and re-ingest.
    log.debug(
        "token_for_outcome.fallback",
        market_id=getattr(market, "market_id", "?"),
        outcome=outcome,
        reason="outcomes_missing",
    )
    return yes_tid


def _enabled_tag_map() -> dict[str, list[str]]:
    """category -> [tag-slugs] for currently-enabled categories (YAML)."""
    cats = categories_cfg.get().get("categories", {})
    return {cat: (c.get("tags") or []) for cat, c in cats.items() if c.get("enabled")}


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
    cat = classify_market(
        tags=tags, question=m.get("question"), slug=m.get("slug"),
        tag_map=_enabled_tag_map(),
    )

    tokens = _parse_json_list(m.get("clobTokenIds"))
    outcomes_list = _parse_json_list(m.get("outcomes"))

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
            event_id=event_id_from_gamma(m),
            end_date=end_dt,
            resolved=bool(m.get("closed")),
            outcome=m.get("outcome"),
            liquidity_usdc=float(m.get("liquidity") or 0),
            volume_24h_usdc=float(m.get("volume24hr") or 0),
            yes_token_id=tokens[0] if len(tokens) > 0 else None,
            no_token_id=tokens[1] if len(tokens) > 1 else None,
            outcomes=outcomes_list or None,
            updated_at=datetime.now(tz=timezone.utc),
        ).on_conflict_do_nothing(index_elements=["market_id"]))
        out = (await s.execute(
            select(Market).where(Market.market_id == market_id)
        )).scalar_one_or_none()

    log.info("market_resolved_jit", market=market_id, category=cat,
             question=(m.get("question") or "")[:60])
    return out
