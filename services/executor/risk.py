"""Pre-flight risk checks. Run before any order, paper or live."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select

from polybot.asset_direction import CRYPTO_MAJORS, asset_of, direction, range_bet, same_bracket
from polybot.politics_candidate import candidate_of
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


async def _held_outcomes(s, *, mode: str, market_id: str) -> set[str]:
    """Outcomes we currently have exposure to in ``market_id`` for ``mode``.

    Paper tracks Position rows (a close zeroes ``size_shares``), so we read
    live net holdings there. Live writes only Fill rows — there's no position
    lifecycle on the live path yet — so a prior non-rejected live BUY on an
    outcome counts as still-held. Returns upper-cased outcome labels.
    """
    if mode == "live":
        rows = (await s.execute(
            select(func.distinct(Fill.outcome)).where(
                Fill.mode == "live",
                Fill.market_id == market_id,
                Fill.side == "BUY",
                Fill.status.in_(("filled", "submitted", "partial")),
            )
        )).scalars().all()
    else:
        rows = (await s.execute(
            select(func.distinct(Position.outcome)).where(
                Position.market_id == market_id,
                func.abs(Position.size_shares) > 0,
            )
        )).scalars().all()
    return {str(o).upper() for o in rows if o}


async def _asset_conflict(s, *, mode: str, market_id: str,
                          outcome: str, side: str) -> tuple[str, str] | None:
    """One-sided-per-asset check.

    Return ``(asset, want_dir)`` if placing this order would put us on the
    OPPOSITE price direction of a still-open position on the same underlying
    crypto asset (e.g. an open "BTC up" bet while this order is "BTC below
    $X"); otherwise None.

    Best-effort and PRECISION-biased: returns None on any ambiguity (non-crypto
    market, unparseable asset/direction) so the caller fails open. Only markets
    that are still OPEN (``end_date`` in the future) constrain new orders — once
    a daily market resolves it stops blocking the next day's fresh bet, so daily
    BTC up/down keeps trading day-to-day; we only forbid holding both sides at
    the same time.
    """
    row = (await s.execute(
        select(Market.question, Market.slug, Market.category)
        .where(Market.market_id == market_id)
    )).first()
    if not row:
        return None
    q, slug, cat = row
    if str(cat or "").lower() != "crypto":
        return None
    asset = asset_of(q, slug)
    if asset is None:
        return None
    want_dir = direction(q, slug, outcome, side)        # bull / bear / None
    want_rng = range_bet(q, slug, outcome, side)        # (stance, lo, hi) / None
    if want_dir is None and want_rng is None:
        return None

    now = datetime.now(tz=timezone.utc)
    if mode == "live":
        # Live path is long-only and writes only Fill rows; a non-rejected BUY
        # on an open crypto market counts as still-held exposure.
        rows = (await s.execute(
            select(Fill.market_id, Market.question, Market.slug, Fill.outcome,
                   Fill.side, Fill.notional_usdc)
            .join(Market, Market.market_id == Fill.market_id)
            .where(
                Fill.mode == "live",
                Fill.side == "BUY",
                Fill.status.in_(("filled", "submitted", "partial")),
                Market.category == "crypto",
                Market.end_date > now,
            )
        )).all()
        held = [(hmid, hq, hs, ho, hsd, float(hn or 0.0))
                for (hmid, hq, hs, ho, hsd, hn) in rows]
    else:
        rows = (await s.execute(
            select(Position.market_id, Market.question, Market.slug,
                   Position.outcome, func.abs(Position.size_shares) * Position.avg_price)
            .join(Market, Market.market_id == Position.market_id)
            .where(
                func.abs(Position.size_shares) > 0,
                Market.category == "crypto",
                Market.end_date > now,
            )
        )).all()
        held = [(hmid, hq, hs, ho, "BUY", float(hn or 0.0))
                for (hmid, hq, hs, ho, hn) in rows]

    # Directional axis (bull/bear) — STRICT: any open opposite-direction leg on
    # the same asset (in a different market) is a conflict. Same-market opposite
    # outcomes are handled earlier by the one-direction-per-market guard.
    if want_dir is not None:
        for hmid, hq, hs, ho, hsd, _hn in held:
            if hmid == market_id:
                continue
            if asset_of(hq, hs) != asset:
                continue
            have = direction(hq, hs, ho, hsd)
            if have is not None and have != want_dir:
                return asset, want_dir

    # Range axis ("between $A-$B") — MAJORITY-WINS: the stance we've committed
    # the most open notional to on a band keeps trading; only the minority
    # opposite stance is blocked. So an aggressive one-sided ladder stays alive
    # while the contradicting side is refused. (Same-market legs are counted —
    # they ARE our commitment to that side.)
    if want_rng is not None:
        same_usdc = 0.0
        opp_usdc = 0.0
        for _hmid, hq, hs, ho, hsd, hn in held:
            if asset_of(hq, hs) != asset:
                continue
            hr = range_bet(hq, hs, ho, hsd)
            if hr is None or not same_bracket(hr, want_rng):
                continue
            if hr[0] == want_rng[0]:
                same_usdc += hn
            else:
                opp_usdc += hn
        if opp_usdc > same_usdc:
            return asset, f"range_{want_rng[0]}"

    return None


async def _crypto_timeframe_conflict(s, *, mode: str, market_id: str,
                                     outcome: str, side: str) -> tuple[str, str, str, str] | None:
    """Cross-asset directional consistency among correlated crypto MAJORS,
    bucketed by resolution DAY (UTC).

    Crypto majors move together, so an open "BTC down today" while a new order is
    "ETH up today" is a self-cancelling thesis (one signal is almost certainly
    noise). Return ``(asset, want_dir, have_asset, day)`` if this order's
    direction OPPOSES an already-open major-crypto leg resolving the SAME UTC day;
    otherwise None.

    Like ``_asset_conflict`` this is best-effort and PRECISION-biased: it returns
    None on any ambiguity (non-crypto, non-major, unparseable direction, missing
    end_date) so the caller fails open. Only still-OPEN markets (``end_date`` in
    the future) constrain new orders, and only legs resolving the same calendar
    day count — so day-to-day directional bets keep trading; we only forbid
    holding opposing directions across majors within one day.
    """
    row = (await s.execute(
        select(Market.question, Market.slug, Market.category, Market.end_date)
        .where(Market.market_id == market_id)
    )).first()
    if not row:
        return None
    q, slug, cat, end_date = row
    if str(cat or "").lower() != "crypto" or end_date is None:
        return None
    asset = asset_of(q, slug)
    if asset is None or asset not in CRYPTO_MAJORS:
        return None
    want_dir = direction(q, slug, outcome, side)        # bull / bear / None
    if want_dir is None:
        return None
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    want_day = end_date.astimezone(timezone.utc).date()

    now = datetime.now(tz=timezone.utc)
    if mode == "live":
        # Live path is long-only and writes only Fill rows; a non-rejected BUY on
        # an open crypto market counts as still-held exposure.
        rows = (await s.execute(
            select(Market.question, Market.slug, Fill.outcome, Fill.side,
                   Market.end_date, Fill.market_id)
            .join(Market, Market.market_id == Fill.market_id)
            .where(
                Fill.mode == "live",
                Fill.side == "BUY",
                Fill.status.in_(("filled", "submitted", "partial")),
                Market.category == "crypto",
                Market.end_date > now,
            )
        )).all()
        held = [(hq, hs, ho, hsd, hed, hmid)
                for (hq, hs, ho, hsd, hed, hmid) in rows]
    else:
        rows = (await s.execute(
            select(Market.question, Market.slug, Position.outcome,
                   Market.end_date, Position.market_id)
            .join(Market, Market.market_id == Position.market_id)
            .where(
                func.abs(Position.size_shares) > 0,
                Market.category == "crypto",
                Market.end_date > now,
            )
        )).all()
        held = [(hq, hs, ho, "BUY", hed, hmid)
                for (hq, hs, ho, hed, hmid) in rows]

    for hq, hs, ho, hsd, hed, hmid in held:
        if hmid == market_id:
            continue                       # same market — one_direction_per_market handles it
        h_asset = asset_of(hq, hs)
        if h_asset is None or h_asset not in CRYPTO_MAJORS or hed is None:
            continue
        if hed.tzinfo is None:
            hed = hed.replace(tzinfo=timezone.utc)
        if hed.astimezone(timezone.utc).date() != want_day:
            continue                       # different resolution day — separate book
        have = direction(hq, hs, ho, hsd)
        if have is not None and have != want_dir:
            return asset, want_dir, h_asset, want_day.isoformat()

    return None


async def _politics_candidate_held(s, *, mode: str, market_id: str) -> tuple[str, str] | None:
    """One-position-per-politics-candidate check.

    If the incoming market names a (non-excluded) candidate we ALREADY hold an
    open position on in a DIFFERENT politics market, return ``(candidate, held_mid)``;
    otherwise None. Keyed on the candidate NAME parsed from the question (Trump
    excluded), so it links markets that share no Polymarket event_id — the gap the
    one-position-per-event guard can't cover. Precision-biased: returns None on any
    ambiguity (non-politics market, unparseable or excluded candidate name) so the
    caller fails open. Only still-OPEN markets (``end_date`` in the future)
    constrain new orders, so a resolved race never blocks a fresh, unrelated entry.
    """
    row = (await s.execute(
        select(Market.question, Market.slug, Market.category)
        .where(Market.market_id == market_id)
    )).first()
    if not row:
        return None
    q, slug, cat = row
    if str(cat or "").lower() != "politics":
        return None
    cand = candidate_of(q, slug)
    if cand is None:
        return None

    now = datetime.now(tz=timezone.utc)
    if mode == "live":
        rows = (await s.execute(
            select(Market.question, Market.slug, Fill.market_id)
            .join(Market, Market.market_id == Fill.market_id)
            .where(
                Fill.mode == "live",
                Fill.side == "BUY",
                Fill.status.in_(("filled", "submitted", "partial")),
                Market.category == "politics",
                Market.market_id != market_id,
                Market.end_date > now,
            )
        )).all()
    else:
        rows = (await s.execute(
            select(Market.question, Market.slug, Position.market_id)
            .join(Market, Market.market_id == Position.market_id)
            .where(
                func.abs(Position.size_shares) > 0,
                Market.category == "politics",
                Market.market_id != market_id,
                Market.end_date > now,
            )
        )).all()

    for hq, hs, hmid in rows:
        if hmid == market_id:
            continue                       # same market — one_direction_per_market handles it
        if candidate_of(hq, hs) == cand:
            return cand, hmid

    return None


async def _event_already_held(s, *, mode: str, market_id: str,
                              event_id: str) -> str | None:
    """One-position-per-event check.

    Return the market_id of an OPEN position in a DIFFERENT market of the same
    Polymarket event, or None. Lets the bot hold at most one market per event
    so it can't take multiple (often offsetting) positions on the same
    underlying — e.g. NO on two frontrunners in one primary. Only still-open
    sibling markets (``end_date`` in the future) count, so a resolved sibling
    never blocks a fresh, unrelated entry.
    """
    now = datetime.now(tz=timezone.utc)
    if mode == "live":
        row = (await s.execute(
            select(Fill.market_id)
            .join(Market, Market.market_id == Fill.market_id)
            .where(
                Fill.mode == "live",
                Fill.side == "BUY",
                Fill.status.in_(("filled", "submitted", "partial")),
                Market.event_id == event_id,
                Market.market_id != market_id,
                Market.end_date > now,
            ).limit(1)
        )).first()
    else:
        row = (await s.execute(
            select(Position.market_id)
            .join(Market, Market.market_id == Position.market_id)
            .where(
                func.abs(Position.size_shares) > 0,
                Market.event_id == event_id,
                Market.market_id != market_id,
                Market.end_date > now,
            ).limit(1)
        )).first()
    return row[0] if row else None


async def preflight(*, mode: str, market_id: str, category: str | None,
                    side: str, size_usdc: float, score: float,
                    outcome: str | None = None) -> dict:
    """Returns {"ok": True, ...} or raises RiskRejection.

    `mode` (paper|live) is the caller's declared mode (executor's
    settings.trading_mode). We override with the runtime mode from
    Redis so dashboard switches take effect on the very next preflight
    — without restarting the executor. Risk config is also per-mode
    merged so live mode's tighter caps apply when the runtime mode is
    "live".

    `outcome` enables the one-direction-per-market guard (skipped when None,
    e.g. legacy/test callers).
    """
    # Caller's declared exec mode (paper|live) — decides which ledger we check
    # for existing exposure below. `mode` itself gets overwritten by the
    # runtime override just below (that override is for cap selection), so
    # capture it first.
    order_mode = mode
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
        # One-direction-per-market: refuse the OPPOSITE outcome of a market we
        # already hold. Mirroring smart money can fire BUY YES *and* BUY NO on
        # the same event; taking both hedges the bot into a guaranteed
        # post-fee loss. We hold at most ONE outcome per market. Disable with
        # position.one_direction_per_market: false.
        if outcome and pos_cfg.get("one_direction_per_market", True):
            want = outcome.upper()
            held = await _held_outcomes(s, mode=order_mode, market_id=market_id)
            if any(o != want for o in held):
                raise RiskRejection(
                    f"opposing_outcome:{market_id[:14]}:have={sorted(held)}:want={want}")

        # One-direction-per-ASSET: refuse a bet that contradicts an open
        # position on the same underlying crypto asset across DIFFERENT
        # markets (e.g. open "BTC up daily" + new "BTC below $X"). Keeps the
        # book uniformly one-sided per asset. Fail-OPEN on any error so a parse
        # bug can never wedge the executor. Disable with
        # position.one_direction_per_asset: false.
        if outcome and pos_cfg.get("one_direction_per_asset", True):
            try:
                conflict = await _asset_conflict(
                    s, mode=order_mode, market_id=market_id,
                    outcome=outcome, side=side)
            except Exception as exc:  # noqa: BLE001
                log.warning("asset_conflict_check_failed",
                            market_id=market_id, err=str(exc))
                conflict = None
            if conflict:
                asset, want_dir = conflict
                raise RiskRejection(f"asset_conflict:{asset}:want={want_dir}")

        # One-direction-per-CRYPTO-TIMEFRAME: among correlated majors (BTC/ETH/
        # SOL/...) resolving the SAME UTC day, refuse a bet whose direction opposes
        # an already-open major-crypto leg. Majors move together, so "BTC down
        # today" + "ETH up today" is a self-cancelling thesis. Majors-only
        # (memecoins exempt), day-bucketed so different horizons don't cross-block,
        # and fail-OPEN on any error. Disable with
        # position.one_direction_per_crypto_timeframe: false.
        if outcome and pos_cfg.get("one_direction_per_crypto_timeframe", True):
            try:
                xconf = await _crypto_timeframe_conflict(
                    s, mode=order_mode, market_id=market_id,
                    outcome=outcome, side=side)
            except Exception as exc:  # noqa: BLE001
                log.warning("crypto_timeframe_conflict_check_failed",
                            market_id=market_id, err=str(exc))
                xconf = None
            if xconf:
                asset, want_dir, have_asset, day = xconf
                raise RiskRejection(
                    f"crypto_timeframe_conflict:{asset}_{want_dir}_vs_{have_asset}:day={day}")

        # One-position-per-EVENT: hold at most ONE market per Polymarket event,
        # so the bot can't take multiple (often offsetting) positions on the
        # same underlying event — e.g. "NO on Bores" + "NO on Lasher" in one
        # NY-12 primary. The first market entered in an event wins; its open
        # siblings are blocked. Fail-OPEN on any error. Markets with no parent
        # event (event_id NULL) are unaffected. Disable with
        # position.one_position_per_event: false.
        if outcome and pos_cfg.get("one_position_per_event", True):
            try:
                ev_row = (await s.execute(
                    select(Market.event_id).where(Market.market_id == market_id)
                )).first()
                event_id = ev_row[0] if ev_row else None
                held_mid = (
                    await _event_already_held(
                        s, mode=order_mode, market_id=market_id, event_id=event_id)
                    if event_id else None
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("event_conflict_check_failed",
                            market_id=market_id, err=str(exc))
                held_mid = None
            if held_mid:
                raise RiskRejection(
                    f"event_conflict:{str(event_id)[:18]}:held={held_mid[:12]}")

        # One-position-per-POLITICS-CANDIDATE: hold at most ONE open bet per
        # candidate. The bot mirrors smart money per-market and otherwise stacks
        # several (often contradictory) bets on the same person across different
        # markets/events — e.g. an open "X win by 5-10%" position while it keeps
        # placing "X not president" orders. Keyed on the candidate NAME parsed from
        # the question (so it links markets that share no event_id); Trump is
        # excluded (his name spans too many unrelated markets). Fail-OPEN on any
        # error or unparseable name. Disable with
        # position.one_position_per_politics_candidate: false.
        if outcome and pos_cfg.get("one_position_per_politics_candidate", True):
            try:
                pc = await _politics_candidate_held(
                    s, mode=order_mode, market_id=market_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("politics_candidate_check_failed",
                            market_id=market_id, err=str(exc))
                pc = None
            if pc:
                cand, held_pmid = pc
                raise RiskRejection(
                    f"politics_candidate:{cand}:held={held_pmid[:12]}")

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
