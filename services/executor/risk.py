"""Pre-flight risk checks. Run before any order, paper or live."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select

from polybot.clients import ClobClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Fill, Market, Position
from polybot.redis_bus import client as redis_client  # noqa: F401  (re-exported for callers)
from polybot.redis_bus import kill_status
from polybot.runtime_config import current_mode, merged_risk

log = get_logger(__name__)


class RiskRejection(Exception):
    pass


async def _spread_pct(token_id: str | None) -> float | None:
    """Return (best_ask - best_bid) / midpoint * 100, or None if book unusable."""
    if not token_id:
        return None
    c = ClobClient()
    try:
        book = await c.book(token_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("risk_spread_book_failed", err=str(exc))
        return None
    finally:
        await c.close()

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None
    try:
        best_bid = max(float(l["price"]) for l in bids if "price" in l)
        best_ask = min(float(l["price"]) for l in asks if "price" in l)
    except (ValueError, KeyError):
        return None
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None
    return (best_ask - best_bid) / mid * 100.0


async def preflight(*, mode: str, market_id: str, category: str | None,
                    side: str, size_usdc: float, score: float) -> dict:
    """Returns {"ok": True, ...} or raises RiskRejection.

    `mode` (paper|live) is the caller's declared mode (executor's
    settings.trading_mode). We override with the runtime mode from
    Redis so dashboard switches take effect on the very next preflight
    — without restarting the executor. Risk config is also per-mode
    merged so live mode's tighter caps apply when the runtime mode is
    "live".
    """
    runtime_mode = await current_mode()
    if runtime_mode != mode:
        # Runtime override (dashboard flip) supersedes the boot-time mode.
        # Important: the EXECUTION path still uses the caller's `mode` for
        # things like Fill.mode = "paper" vs "live" — but the RISK CAPS
        # come from the runtime mode so live-mode limits apply the moment
        # the operator flips the switch.
        mode = runtime_mode
    cfg = await merged_risk(mode)
    pos_cfg = cfg.get("position", {})
    dd_cfg = cfg.get("drawdown", {})
    exec_cfg = cfg.get("execution", {})

    # 0) input sanity — refuse non-positive sizes, garbage sides, or
    #    NaN/Inf. Without these the upper-bound checks below pass a
    #    negative `size_usdc` since the LHS is always smaller than the
    #    cap, leaving an attacker-pushed Redis payload able to walk
    #    straight through risk. Validate side too — `place_limit`
    #    accepts unknown sides as BUY in some venues.
    if size_usdc is None or not (size_usdc > 0):
        raise RiskRejection(f"non_positive_size:{size_usdc}")
    if size_usdc != size_usdc or size_usdc in (float("inf"), float("-inf")):
        raise RiskRejection(f"size_not_finite:{size_usdc}")
    if side not in ("BUY", "SELL"):
        raise RiskRejection(f"bad_side:{side!r}")

    # 1) kill switch
    k = await kill_status()
    if k:
        raise RiskRejection(f"kill_switch_active:{k}")

    # 2) per-order size
    max_pos = float(pos_cfg.get("max_position_usdc", 25.0))
    if size_usdc > max_pos:
        raise RiskRejection(f"size>{max_pos}")

    # 3) per-market cap — sum absolute notional exposure on this market.
    async with session_scope() as s:
        existing = (await s.execute(
            select(func.coalesce(
                func.sum(func.abs(Position.size_shares) * Position.avg_price), 0.0))
            .where(Position.market_id == market_id)
        )).scalar_one()
        if existing + size_usdc > float(pos_cfg.get("max_per_market_usdc", max_pos)):
            raise RiskRejection(f"per_market_cap:{existing}+{size_usdc}")

        # 4) per-category cap — sum notional across all markets sharing the
        #    current signal's category. We resolve the category here as a
        #    fallback for callers that pass None.
        cat = category
        if cat is None:
            cat_row = (await s.execute(
                select(Market.category).where(Market.market_id == market_id)
            )).first()
            cat = cat_row[0] if cat_row else None

        max_per_cat = pos_cfg.get("max_per_category_usdc")
        if cat and max_per_cat is not None:
            cat_existing = (await s.execute(
                select(func.coalesce(
                    func.sum(func.abs(Position.size_shares) * Position.avg_price), 0.0))
                .select_from(Position)
                .join(Market, Market.market_id == Position.market_id)
                .where(Market.category == cat)
            )).scalar_one()
            if cat_existing + size_usdc > float(max_per_cat):
                raise RiskRejection(
                    f"per_category_cap:{cat}:{cat_existing}+{size_usdc}>{max_per_cat}")

        # 5) max open positions — count any market with non-zero net exposure,
        #    in either direction (short or long).
        open_n = (await s.execute(
            select(func.count(func.distinct(Position.market_id)))
            .where(func.abs(Position.size_shares) > 0)
        )).scalar_one()
        if open_n >= int(pos_cfg.get("max_open_positions", 5)):
            raise RiskRejection(f"max_open_positions:{open_n}")

        # 6) daily loss — relies on paper.py / live close logic writing
        #    realized_pnl_usdc onto the Position row when shares are closed.
        today = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        realised_today = (await s.execute(
            select(func.coalesce(func.sum(Position.realized_pnl_usdc), 0.0))
            .where(Position.updated_at >= today)
        )).scalar_one()
        if realised_today <= -float(dd_cfg.get("max_daily_loss_usdc", 50.0)):
            raise RiskRejection(f"daily_loss_breached:{realised_today}")

        # 7) order rate (last 60s).
        #    Count fills regardless of mode column — the executor may have
        #    been booted in paper mode and runtime-flipped to live, so the
        #    Fill.mode bucket lags the runtime mode by one row until the
        #    main loop refreshes. Counting across modes still caps total
        #    submission velocity which is what the gate is for.
        # Count only ACTUAL placements toward the budget. Counting
        # rejected/settled rows lets a rejection storm self-DOS the
        # executor: every reject increments the bucket, every subsequent
        # signal then trips rate_limit, locking the bot out. SETTLE rows
        # are auto-generated by pnl_loop and shouldn't consume budget
        # either.
        rate_cap = int(exec_cfg.get("max_orders_per_minute", 6))
        recent = (await s.execute(
            select(func.count(Fill.id)).where(
                Fill.ts >= datetime.now(tz=timezone.utc) - timedelta(seconds=60),
                Fill.status.in_(("filled", "partial", "submitted")),
            )
        )).scalar_one()
        if recent >= rate_cap:
            raise RiskRejection(f"rate_limit:{recent}>={rate_cap}")

    # 8) score floor — pure defence-in-depth. The signals engine has already
    #    applied the real, category-aware score threshold; this floor only
    #    catches replayed/corrupted/stale messages that somehow surface with
    #    near-zero scores. Keep it well below any legitimate engine threshold
    #    so we don't double-gate genuine clusters.
    if score < 0.005:
        raise RiskRejection(f"score_too_low:{score}")

    # 9) spread check (live only) — refuse to send into a blown-out book.
    spread_limit = exec_cfg.get("reject_if_spread_pct_above")
    if mode == "live" and spread_limit is not None:
        async with session_scope() as s:
            row = (await s.execute(
                select(Market.yes_token_id, Market.no_token_id)
                .where(Market.market_id == market_id)
            )).first()
        token_id = None
        if row:
            # spread is symmetric across YES/NO; pick whichever side we have.
            token_id = row[0] or row[1]
        spread = await _spread_pct(token_id)
        if spread is not None and spread > float(spread_limit):
            raise RiskRejection(f"spread_too_wide:{spread:.2f}%>{spread_limit}%")

    return {"ok": True, "max_size": max_pos}
