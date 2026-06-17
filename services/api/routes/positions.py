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

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from polybot import alerts
from polybot.clients import ClobClient
from polybot.db import get_session, session_scope
from polybot.logging import get_logger
from polybot.market_resolver import token_for_outcome
from polybot.models import AuditLog, Market, Position
from polybot.redis_bus import publish
from services.api.deps import require_admin
from services.executor.paper import close_position as paper_close_position

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


@router.get("", dependencies=[Depends(require_admin)])
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


# ---------------- Manual close endpoints ------------------------------------
#
# Paper-mode close: walks the live orderbook bids and sells at the best
# available prices. Realized PnL credited immediately.
#
# Live-mode close: NOT IMPLEMENTED YET — would need to place a real SELL
# order via place_live(side="SELL"). For now we return 501 so the operator
# isn't tricked into thinking they liquidated something they didn't.


class ClosePositionBody(BaseModel):
    market_id: str = Field(min_length=10)
    outcome: str = Field(min_length=1)
    fraction: float = Field(default=1.0, ge=0.01, le=1.0)


@router.post("/close", dependencies=[Depends(require_admin)])
async def close_one_position(
    body: ClosePositionBody = Body(...),
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Close a single open position immediately at best bid (paper only).

    `fraction` lets the operator partial-close (e.g. 0.5 = sell half).
    Defaults to full close.

    Returns the resulting Fill record or a rejection reason. Audited
    BEFORE the close-attempt — even a crash mid-close leaves a record
    of who tried what. The audit-after-side-effects pattern that lived
    here lost the intent on any exception in paper_close_position.
    """
    # Resolve actor BEFORE the side effect so it survives crashes.
    from services.api.rate_limit import _client_ip as _safe_ip
    actor = f"admin@{_safe_ip(request)}" if request else "admin"

    # Step 1: write an INTENT row so we know who attempted this close
    # even if the side effect crashes the request.
    try:
        async with session_scope() as s:
            s.add(AuditLog(
                actor=actor,
                event="position_close_attempt",
                payload={
                    "market_id": body.market_id,
                    "outcome": body.outcome,
                    "fraction": body.fraction,
                },
            ))
    except Exception:  # noqa: BLE001
        log.exception("close_one_audit_attempt_failed")

    # Step 2: execute the close.
    result = await paper_close_position(
        market_id=body.market_id,
        outcome=body.outcome,
        fraction=body.fraction,
    )

    # Step 3: write the RESULT row + publish for the dashboard fill stream.
    try:
        async with session_scope() as s:
            s.add(AuditLog(
                actor=actor,
                event="position_close_result",
                payload={
                    "market_id": body.market_id,
                    "outcome": body.outcome,
                    "fraction": body.fraction,
                    "result_status": result.get("status") if isinstance(result, dict) else None,
                    "reason": result.get("reason") if isinstance(result, dict) else None,
                },
            ))
        await publish("fill:new", {"signal_id": None, "result": result, "mode": "paper",
                                    "source": "manual_close"})
    except Exception:  # noqa: BLE001
        log.exception("close_one_audit_result_failed")
    return result


@router.post("/close-all", dependencies=[Depends(require_admin)])
async def close_all_positions() -> dict[str, Any]:
    """EMERGENCY: close EVERY open paper position at best bid.

    Walks all positions with size_shares > 0 in serial (so we don't blow up
    the CLOB rate limit). Returns a summary of per-position results.
    Audit-logged with the full list so post-incident reconstruction is
    possible. Use with care — partial closes are NOT rolled back if one
    market mid-way fails.
    """
    async with session_scope() as s:
        rows = (await s.execute(
            select(Position.market_id, Position.outcome, Position.size_shares)
            .where(Position.size_shares > 0)
        )).all()

    if not rows:
        return {"closed": [], "rejected": [], "total": 0,
                "summary": "no open positions"}

    closed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for r in rows:
        try:
            result = await paper_close_position(
                market_id=r.market_id, outcome=r.outcome, fraction=1.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("close_all_iter_failed",
                          market=r.market_id, outcome=r.outcome)
            rejected.append({"market_id": r.market_id, "outcome": r.outcome,
                             "error": f"{type(exc).__name__}:{exc}"})
            continue
        # Per-iteration audit + publish — without this, a crash mid-loop
        # leaves DB positions closed with no forensic record of which
        # closes succeeded and no live-feed event for the dashboard.
        try:
            await publish("fill:new", {"signal_id": None, "result": result,
                                        "mode": "paper", "source": "close_all"})
        except Exception:  # noqa: BLE001
            log.exception("close_all_publish_failed")
        if isinstance(result, dict) and result.get("status") in (
            "filled", "submitted", "partial"
        ):
            closed.append({"market_id": r.market_id, "outcome": r.outcome,
                           "result": result})
        else:
            rejected.append({"market_id": r.market_id, "outcome": r.outcome,
                             "result": result})

    summary = {"closed": closed, "rejected": rejected,
               "total": len(rows),
               "closed_n": len(closed), "rejected_n": len(rejected)}

    try:
        async with session_scope() as s:
            s.add(AuditLog(
                actor="emergency_close_all",
                event="close_all_positions",
                payload={
                    "total": len(rows),
                    "closed": len(closed),
                    "rejected": len(rejected),
                    "rejected_markets": [
                        {"market_id": r["market_id"], "outcome": r["outcome"]}
                        for r in rejected
                    ][:50],  # cap to keep audit row small
                },
            ))
        await alerts.notify(
            "warn",
            "Emergency close-all triggered",
            f"{len(closed)} closed, {len(rejected)} rejected of {len(rows)} positions",
        )
    except Exception:  # noqa: BLE001
        log.exception("close_all_audit_failed")

    return summary
