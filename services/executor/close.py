"""Position-closing execution for the exit-mirror (Stage 3).

Closing a LIVE position is deliberately conservative:
  1. Cancel our resting unfilled BUY orders on the market/outcome (always safe —
     removing our own orders can't create exposure; cancelling one that already
     filled is a harmless venue no-op).
  2. Sell the shares we ACTUALLY hold on the venue. Sizing comes from the data-api
     position (ground truth via live_shares_held), NOT the local Fill ledger — the
     live path is fire-and-forget and never reconciles a 'submitted' Fill, so the
     ledger can't tell resting from filled and would risk overselling into a naked
     short. We floor the size so we can never request more than we hold.

Residual safety (kill-switch with the allow_close_when_killed bypass, and the
order-rate budget) is enforced here rather than via the entry preflight, so the
close path has no dependency on the entry-gating logic.

Paper closing reuses the reliable paper Position lifecycle (paper.close_position).
Live closing is gated by exit_mirror.live_enabled at the caller (exit_loop).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from polybot.clients import ClobClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Fill
from polybot.redis_bus import kill_status
from polybot.runtime_config import merged_risk
from services.executor import paper as paper_mod
from services.executor.live import MIN_SHARES, live_shares_held, place_live_shares

log = get_logger(__name__)


async def _cancel_resting_buys(market_id: str, outcome: str) -> int:
    """Cancel our live resting/unfilled BUY orders on (market, outcome) and mark
    them ``cancelled`` so they drop out of exposure queries. Returns count
    cancelled. Always safe; failures on individual orders are logged and skipped."""
    want = (outcome or "").upper()
    async with session_scope() as s:
        rows = (await s.execute(
            select(Fill.id, Fill.venue_order_id).where(
                Fill.mode == "live",
                Fill.market_id == market_id,
                func.upper(Fill.outcome) == want,
                Fill.side == "BUY",
                Fill.status.in_(("submitted", "partial")),
                Fill.venue_order_id.isnot(None),
            )
        )).all()
    if not rows:
        return 0
    c = ClobClient()
    n = 0
    try:
        for fill_id, voi in rows:
            try:
                await c.cancel(str(voi))
            except Exception as exc:  # noqa: BLE001
                log.warning("close_cancel_failed", venue_order_id=str(voi), err=str(exc))
                continue
            async with session_scope() as s:
                await s.execute(
                    update(Fill).where(Fill.id == fill_id).values(status="cancelled"))
            n += 1
    finally:
        await c.close()
    if n:
        log.info("close_cancelled_resting", market_id=market_id, outcome=want, n=n)
    return n


async def _close_blocked_reason(market_id: str) -> str | None:
    """Residual safety for a close: kill switch (with allow_close_when_killed
    bypass) + order-rate budget. Returns a rejection reason, or None to proceed."""
    cfg = await merged_risk("live")
    exit_cfg = cfg.get("exit_mirror", {})
    exec_cfg = cfg.get("execution", {})
    k = await kill_status()
    if k and not bool(exit_cfg.get("allow_close_when_killed", True)):
        return f"kill_switch_active_exit_blocked:{k}"
    if k:
        log.info("kill_bypass_for_close", market_id=market_id, kill=str(k))
    rate_cap = int(exec_cfg.get("max_orders_per_minute", 6))
    async with session_scope() as s:
        recent = (await s.execute(
            select(func.count(Fill.id)).where(
                Fill.ts >= datetime.now(tz=timezone.utc) - timedelta(seconds=60),
                Fill.status.in_(("filled", "partial", "submitted")),
            )
        )).scalar_one()
    if recent >= rate_cap:
        return f"rate_limit_exit:{recent}>={rate_cap}"
    return None


async def close_live(*, market_id: str, outcome: str, signal_id: int | None = None,
                     urgent: bool = True) -> dict:
    """Exit a LIVE position: cancel resting BUYs, then sell the shares actually
    held on the venue. Never sells more than held (no naked short)."""
    cancelled = await _cancel_resting_buys(market_id, outcome)

    # Authoritative held shares from the venue (NOT the unreconciled Fill ledger).
    held = await live_shares_held(market_id, outcome)
    if held is None:
        log.warning("close_live_venue_read_failed", market_id=market_id, outcome=outcome)
        return {"status": "venue_read_failed", "cancelled": cancelled}
    if held < MIN_SHARES:
        status = "cancelled_only" if cancelled else "no_position"
        log.info("close_live_nothing_to_sell", market_id=market_id, outcome=outcome,
                 held=held, cancelled=cancelled, status=status)
        return {"status": status, "cancelled": cancelled, "held": held}

    blocked = await _close_blocked_reason(market_id)
    if blocked:
        log.warning("close_live_blocked", market_id=market_id, outcome=outcome, reason=blocked)
        return {"status": "blocked", "reason": blocked, "cancelled": cancelled, "held": held}

    # Floor to 2 dp so we never request MORE than we hold (no overshoot → no naked
    # short); leaves at most ~$0.01 of dust unsold.
    shares = math.floor(held * 100) / 100.0
    resp = await place_live_shares(
        signal_id=signal_id, market_id=market_id, outcome=outcome, side="SELL",
        shares=shares, order_kind=("taker" if urgent else "maker"))
    resp = dict(resp) if isinstance(resp, dict) else {"status": str(resp)}
    resp["cancelled"] = cancelled
    resp["sold_shares"] = shares
    log.info("close_live_done", market_id=market_id, outcome=outcome,
             sold_shares=shares, cancelled=cancelled, status=resp.get("status"))
    return resp


async def close_paper(*, market_id: str, outcome: str) -> dict:
    """Exit a PAPER position via the reliable paper Position lifecycle (full exit)."""
    try:
        return await paper_mod.close_position(market_id, outcome, fraction=1.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("close_paper_failed", market_id=market_id, outcome=outcome, err=str(exc))
        return {"status": "error", "error": str(exc)}
