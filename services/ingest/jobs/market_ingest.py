"""Pull active markets from Gamma and upsert into `markets`."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.categorize import classify_market
from polybot.clients import GammaClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.market_resolver import event_id_from_gamma
from polybot.models import Market

log = get_logger(__name__)


async def run_market_ingest() -> None:
    # Same fix as leaderboard_scraper: merged_categories includes Redis
    # overrides so disabling a category via dashboard immediately stops
    # market ingest for it.
    from polybot.runtime_config import merged_categories
    cats = await merged_categories()
    tag_map = {cat: c["tags"] for cat, c in cats.items() if c.get("enabled")}

    g = GammaClient()
    try:
        page_size = 100  # Gamma caps each page at 100 regardless of `limit`
        page = 0
        total = 0
        while True:
            try:
                ms = await g.markets(limit=page_size, offset=page * page_size,
                                     active=True, closed=False)
            except httpx.HTTPStatusError as exc:
                # Gamma returns 422 (not an empty page) once `offset` runs past
                # the end of the available market set — it caps pagination at
                # ~2000. Treat a 4xx on a LATER page as "no more pages" so one
                # tail rejection doesn't fail the whole ingest cycle (markets
                # from earlier pages are already committed per-page). A 4xx on
                # page 0 is a genuine error, so re-raise that.
                if page > 0 and exc.response.status_code in (400, 422):
                    log.info("market_ingest_pagination_end", page=page,
                             offset=page * page_size,
                             status=exc.response.status_code)
                    break
                raise
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
                    cat = classify_market(tags=tags, question=m.get("question"),
                                          slug=m.get("slug"), tag_map=tag_map)

                    # Gamma returns clobTokenIds + outcomes as JSON-encoded
                    # strings: clobTokenIds='["yes_id","no_id"]', outcomes='["Yes","No"]'
                    # for binary; for sports ['"TYLOO","Lynn Vision"'] etc. The
                    # two lists are positionally aligned: outcomes[i] is the
                    # human label for clobTokenIds[i]. This alignment is what
                    # `market_resolver.token_for_outcome()` relies on to map
                    # arbitrary outcome strings to the correct CLOB token.
                    def _parse_json_list(raw: object) -> list[str]:
                        if isinstance(raw, list):
                            return [str(x) for x in raw]
                        if isinstance(raw, str) and raw.startswith("["):
                            try:
                                d = json.loads(raw)
                                return [str(x) for x in d] if isinstance(d, list) else []
                            except json.JSONDecodeError:
                                return []
                        return []

                    tokens = _parse_json_list(m.get("clobTokenIds"))
                    outcomes_list = _parse_json_list(m.get("outcomes"))
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
                        event_id=event_id_from_gamma(m),
                        end_date=end_dt,
                        resolved=bool(m.get("closed")),
                        outcome=m.get("outcome"),
                        liquidity_usdc=float(m.get("liquidity") or 0),
                        volume_24h_usdc=float(m.get("volume24hr") or 0),
                        yes_token_id=yes_tid,
                        no_token_id=no_tid,
                        outcomes=outcomes_list or None,
                        updated_at=datetime.now(tz=timezone.utc),
                    ).on_conflict_do_update(
                        index_elements=["market_id"],
                        set_={"liquidity_usdc": pg_insert(Market).excluded.liquidity_usdc,
                              "volume_24h_usdc": pg_insert(Market).excluded.volume_24h_usdc,
                              "category": pg_insert(Market).excluded.category,
                              # Keep an existing event_id if a later pass lacks
                              # events[] (closed-market re-ingest etc.).
                              "event_id": sa.func.coalesce(
                                  pg_insert(Market).excluded.event_id,
                                  Market.event_id,
                              ),
                              "resolved": pg_insert(Market).excluded.resolved,
                              "outcome": pg_insert(Market).excluded.outcome,
                              # Don't overwrite outcomes with NULL on update —
                              # if a later ingest pass doesn't include them
                              # (closed-market re-ingest etc.), keep the old.
                              "outcomes": sa.func.coalesce(
                                  pg_insert(Market).excluded.outcomes,
                                  Market.outcomes,
                              ),
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
