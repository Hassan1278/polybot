"""Open paper / live positions, joined with market metadata.

What the bot currently holds. Dashboard reads this to render the
"Open positions" card.

For each open position we also try a best-effort live mark price via
CLOB /midpoint so the dashboard shows mark-to-market PnL without
running its own CLOB client. CLOB failures degrade gracefully to
`mark_price = null`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.clients import ClobClient
from polybot.db import get_session
from polybot.logging import get_logger
from polybot.market_resolver import token_for_outcome
from polybot.models import Market, Position

log = get_logger(__name__)
router = APIRouter()


async def _safe_midpoint(c: ClobClient, token_id: str | None) -> float | None:
    """Best-effort mark with hard 3 s timeout per call.

    Uses ClobClient.best_mark which tries /midpoint first then falls back
    to /last-trade-price. The fallback matters for resolved-but-pending
    markets where the orderbook is gone but the last printed trade is the
    resolution price (0.999 / 0.001) — without it our dashboard shows
    None for half the open positions.

    Returns None on total failure / no data so the caller can render "—".
    """
    if not token_id:
        return None
    try:
        async with asyncio.timeout(3.0):
            mark = await c.best_mark(token_id)
        return mark if mark > 0 else None
    except Exception:  # noqa: BLE001
        return None


@router.get("")
async def list_positions(
    *,
    include_closed: bool = False,
    s: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Open positions (size_shares > 0). Set include_closed=true to also
    return positions whose size hit zero — useful for "recent closes".
    """
    q = (
        select(
            Position.wallet,
            Position.market_id,
            Position.outcome,
            Position.size_shares,
            Position.avg_price,
            Position.realized_pnl_usdc,
            Position.updated_at,
            Market.slug,
            Market.question,
            Market.category,
            Market.end_date,
            Market.yes_token_id,
            Market.no_token_id,
            Market.outcomes,
            Market.resolved,
        )
        .join(Market, Market.market_id == Position.market_id, isouter=True)
        .order_by(Position.updated_at.desc())
    )
    if not include_closed:
        q = q.where(Position.size_shares > 0)

    rows = (await s.execute(q)).all()
    if not rows:
        return []

    # Live mark prices — gather concurrently. Each call gets its own 2 s
    # timeout via _safe_midpoint; we add a generous outer 8 s as a backstop.
    # `return_exceptions=True` so one failed token (e.g. resolved market with
    # no book) doesn't blackhole the whole batch.
    c = ClobClient()
    try:
        async def _mark(row: Any) -> float | None:
            # Centralised token-id lookup. Correctly handles:
            #   - YES/NO binary markets
            #   - Multi-outcome markets via markets.outcomes JSONB
            # Falls back to yes_token_id (legacy behaviour) only when no
            # outcomes column is set — old markets that pre-date migration
            # 0003 may need a backfill (scripts/backfill_market_outcomes.py).
            tok = token_for_outcome(row, row.outcome)
            return await _safe_midpoint(c, tok)

        try:
            async with asyncio.timeout(8.0):
                results = await asyncio.gather(
                    *[_mark(r) for r in rows],
                    return_exceptions=True,
                )
            marks = [r if isinstance(r, (float, int)) else None for r in results]
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("positions.mark_lookup_timeout", n=len(rows))
            marks = [None] * len(rows)
    finally:
        await c.close()

    out: list[dict[str, Any]] = []
    for r, mark in zip(rows, marks, strict=True):
        size = float(r.size_shares or 0.0)
        avg = float(r.avg_price or 0.0)
        cost = size * avg
        if mark is not None and size > 0:
            mtm = (mark - avg) * size
            pct = (mark - avg) / avg if avg > 0 else None
        else:
            mtm = None
            pct = None
        out.append({
            "wallet": r.wallet,
            "market_id": r.market_id,
            "slug": r.slug,
            "question": r.question,
            "category": r.category,
            "end_date": r.end_date.isoformat() if isinstance(r.end_date, datetime) else None,
            "outcome": r.outcome,
            "size_shares": size,
            "avg_price": avg,
            "cost_usdc": round(cost, 2),
            "mark_price": mark,
            "mark_to_market_usdc": round(mtm, 2) if mtm is not None else None,
            "pct_change": round(pct, 4) if pct is not None else None,
            "realized_pnl_usdc": float(r.realized_pnl_usdc or 0.0),
            "resolved": bool(r.resolved),
            "updated_at": r.updated_at.isoformat() if isinstance(r.updated_at, datetime) else None,
        })
    return out
