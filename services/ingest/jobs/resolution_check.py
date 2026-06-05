"""Resolution check — flip markets.resolved=true once Polymarket settles them.

Why it exists:
    market_ingest only fetches `active=true&closed=false` markets, so once a
    market resolves, its row in our DB never gets the `resolved=true` flag
    flipped. The executor's settle_resolved_markets loop keys off that flag,
    so open paper positions on resolved markets stay open forever (e.g. the
    NIP CS:GO match — match was played 7 days ago, position still "open").

What it does:
    Every CHECK_INTERVAL_S seconds, scan open paper positions whose market
    end_date is past (or whose end_date is null and were opened > 14 days
    ago). For each, re-query Gamma by condition_id with `closed=true` so
    resolved markets come back. If Gamma says it's resolved, update our
    markets row → the executor's settle loop picks it up on the next minute
    and redeems the position to $1 / $0.

This is a SAFETY net: most resolutions DO flow through market_ingest
indirectly (because they tend to remain `active=true` for a grace period
before being marked closed), but the long-tail / fast-resolution markets
(esports BO3 best-of-three, intra-day sports) need this catch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.clients import GammaClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market, Position

log = get_logger(__name__)

# How old "past end-date" must be before we go probing Gamma — Polymarket
# typically takes minutes-to-hours to flip resolved after end_date, so we
# don't want to hammer the API for markets that just expired.
PROBE_GRACE_MINUTES = 15

# Maximum number of markets we probe per cycle. With 50 max open positions
# this is plenty; protects against runaway API calls if positions count grows.
MAX_PROBE_PER_CYCLE = 50


def _normalise_resolved_payload(m: dict) -> tuple[bool, str | None]:
    """Read Polymarket's market payload and return (is_resolved, winning_outcome).

    Polymarket flags resolved markets via `closed=true`. The winning outcome
    is named in `umaResolutionStatuses[0].outcomeIndex` or in the simpler
    `outcome` field when the resolution is unambiguous. Some payloads carry
    only `closedTime` — we treat presence of that field as a resolution
    signal even if `closed` is absent.
    """
    closed = bool(m.get("closed"))
    has_closed_time = bool(m.get("closedTime"))
    is_resolved = closed or has_closed_time
    outcome = m.get("outcome")
    if not outcome:
        # Try to derive from outcomePrices: the index where price == 1.0 won.
        # outcomePrices comes back as a JSON-encoded string of ["0", "1"] etc.
        prices_raw = m.get("outcomePrices")
        if isinstance(prices_raw, str) and prices_raw.startswith("["):
            import json
            try:
                prices = json.loads(prices_raw)
                outcomes_raw = m.get("outcomes")
                outcomes = (
                    json.loads(outcomes_raw)
                    if isinstance(outcomes_raw, str) and outcomes_raw.startswith("[")
                    else None
                )
                if outcomes and prices:
                    for name, p in zip(outcomes, prices):
                        try:
                            if float(p) >= 0.99:
                                outcome = name
                                break
                        except (TypeError, ValueError):
                            continue
            except (json.JSONDecodeError, ValueError):
                pass
    return is_resolved, outcome


async def _open_position_markets_to_probe(s) -> list[str]:
    """Pick markets that have open paper positions AND look resolved by clock."""
    grace_cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=PROBE_GRACE_MINUTES)
    fallback_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=14)

    rows = (await s.execute(
        select(Position.market_id, Market.end_date, Market.resolved)
        .join(Market, Market.market_id == Position.market_id, isouter=True)
        .where(and_(
            Position.size_shares > 0,
            Position.wallet == "PAPER",
            or_(Market.resolved.is_(False), Market.resolved.is_(None)),
            or_(
                # End-date has passed by at least our grace period
                and_(Market.end_date.is_not(None), Market.end_date < grace_cutoff),
                # No end-date in DB AND the position has been open ≥ 14 days
                and_(Market.end_date.is_(None), Position.updated_at < fallback_cutoff),
            ),
        ))
        .limit(MAX_PROBE_PER_CYCLE)
    )).all()
    return [r[0] for r in rows]


async def run_resolution_check() -> None:
    async with session_scope() as s:
        market_ids = await _open_position_markets_to_probe(s)

    if not market_ids:
        log.info("resolution_check_idle", probed=0)
        return

    g = GammaClient()
    flipped = 0
    try:
        for mid in market_ids:
            try:
                m = await g.market_by_condition_id(mid)
            except Exception as exc:  # noqa: BLE001
                log.warning("resolution_check_fetch_failed", market=mid, err=str(exc))
                continue
            if not m:
                continue

            is_resolved, winning = _normalise_resolved_payload(m)
            if not is_resolved:
                continue

            async with session_scope() as s:
                await s.execute(pg_insert(Market).values(
                    market_id=mid,
                    slug=m.get("slug") or "",
                    question=m.get("question") or "",
                    resolved=True,
                    outcome=winning,
                    updated_at=datetime.now(tz=timezone.utc),
                ).on_conflict_do_update(
                    index_elements=["market_id"],
                    set_={
                        "resolved": True,
                        "outcome": winning,
                        "updated_at": datetime.now(tz=timezone.utc),
                    },
                ))
            flipped += 1
            log.info(
                "resolution_flipped",
                market=mid,
                outcome=winning,
                question=(m.get("question") or "")[:60],
            )
    finally:
        await g.close()

    log.info("resolution_check_done", probed=len(market_ids), flipped=flipped)
