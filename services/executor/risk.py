"""Pre-flight risk checks. Run before any order, paper or live."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select

from polybot.asset_direction import (
    CRYPTO_MAJORS,
    asset_of,
    direction,
    range_bet,
    regions_conflict,
    same_bracket,
    win_region,
)
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


async def _same_day_bucket_conflict(s, *, mode: str, market_id: str,
                                    outcome: str, side: str) -> tuple[str, str, str] | None:
    """At most ONE coherent crypto price bet per asset per resolution DAY (UTC).

    Daily crypto settlements list many overlapping price markets — threshold
    ("above $X") AND range ("between $A-$B") — on the same underlying + day.
    Mirroring smart money the bot would otherwise stack CONTRADICTORY legs, e.g.
    "above 62k NO" (wins <=62k) alongside "between 60-62k NO" (wins <60k OR >62k):
    a self-hedge that only both-wins below 60k and bleeds fees otherwise. The
    directional/range guards (``_asset_conflict`` / ``_crypto_timeframe_conflict``)
    miss this because a threshold bet and a range bet are treated as separate
    axes; and the markets often share no populated ``event_id``, so
    ``_event_already_held`` misses them too.

    Reduce each leg to its WIN-REGION (price intervals where it pays out) and
    return ``(asset, descriptor, held_mid)`` when this order's region CROSSES an
    already-open position's on the same asset + same UTC resolution day (each wins
    where the other loses). Covers threshold-vs-range, range-vs-range and
    threshold-vs-threshold uniformly; nested/identical regions (adding to the same
    thesis) don't conflict.

    Deliberately ``event_id``-INDEPENDENT — keyed on (asset, day, win-region)
    parsed from the question — so it holds even when Gamma doesn't group the
    markets. PRECISION-biased like its siblings: returns None on any ambiguity
    (non-crypto, unparseable asset/price, missing end_date) so the caller fails
    open. Only still-OPEN markets (``end_date`` in the future) constrain new
    orders, so a settled market never blocks the next day's fresh bet.
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
    if asset is None:
        return None
    want_region = win_region(q, slug, outcome, side)    # win intervals / None
    if want_region is None:
        return None                                     # not a parseable price bet — skip
    want_band = range_bet(q, slug, outcome, side)       # for the human reason string
    if want_band is not None:
        descriptor = f"{int(want_band[1])}-{int(want_band[2])}"
    else:
        a0, b0 = want_region[0]                         # threshold: one finite edge
        descriptor = f"thr-{int(b0 if a0 == -math.inf else a0)}"
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
        if asset_of(hq, hs) != asset or hed is None:
            continue
        if hed.tzinfo is None:
            hed = hed.replace(tzinfo=timezone.utc)
        if hed.astimezone(timezone.utc).date() != want_day:
            continue                       # different resolution day — separate book
        held_region = win_region(hq, hs, ho, hsd)
        if held_region is not None and regions_conflict(want_region, held_region):
            return asset, descriptor, hmid

    return None


async def _crypto_factor_exposure(s, *, mode: str, market_id: str,
                                  outcome: str, side: str, size_usdc: float,
                                  cap: float) -> tuple[str, float] | None:
    """Cap GROSS notional on the correlated crypto directional factor.

    Treats every crypto MAJOR as ONE bet per direction — they move together
    (~0.85 intraday), so "BTC down" + "BTC below $X" + "ETH down" are one bet at
    3x size, not three independent positions. Sums open same-direction crypto
    notional and returns ``(want_dir, existing_usdc)`` if adding ``size_usdc``
    would push the factor past ``cap``; else None.

    This is what the per-market / per-asset / per-category caps miss: they bound a
    single market or net a single asset, but nothing bounds the *aggregate
    directional* exposure to the whole correlated complex — exactly the
    concentration that can halve the account on one wrong call. Precision-biased:
    None when the incoming order isn't a directional crypto-major bet (non-crypto,
    no/multiple assets, memecoin, or a range-only / unparseable direction) so the
    caller fails open. Only still-OPEN markets (``end_date`` in the future) count.
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
    if asset is None or asset not in CRYPTO_MAJORS:
        return None
    want_dir = direction(q, slug, outcome, side)        # bull / bear / None
    if want_dir is None:
        return None

    now = datetime.now(tz=timezone.utc)
    if mode == "live":
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

    # Sum same-direction crypto-major notional (incl. this market's own open
    # exposure — it's legitimately part of the factor total).
    total = 0.0
    for _hmid, hq, hs, ho, hsd, hn in held:
        a = asset_of(hq, hs)
        if a is None or a not in CRYPTO_MAJORS:
            continue
        if direction(hq, hs, ho, hsd) == want_dir:
            total += hn
    if total + size_usdc > cap:
        return want_dir, total
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


async def _bet_notional(s, *, mode: str, market_id: str) -> float:
    """Current cumulative notional (USDC) committed to ``market_id`` for ``mode``,
    INCLUDING resting/in-flight orders.

    Paper keeps a Position lifecycle, so its net notional is authoritative. The
    LIVE path writes only Fill rows (no Position), so we sum non-rejected BUY
    fills — ``filled`` + ``submitted`` + ``partial`` — which means a resting
    (unfilled) limit order STILL counts toward the cap. That closes the loophole
    where the old Position-only query summed to ~0 in live and let the bot stack
    many small limit orders on one market past the ceiling (it reached ~$90).
    """
    if mode == "live":
        return float((await s.execute(
            select(func.coalesce(func.sum(Fill.notional_usdc), 0.0)).where(
                Fill.mode == "live",
                Fill.market_id == market_id,
                Fill.side == "BUY",
                Fill.status.in_(("filled", "submitted", "partial")),
            )
        )).scalar_one())
    return float((await s.execute(
        select(func.coalesce(
            func.sum(func.abs(Position.size_shares) * Position.avg_price), 0.0))
        .where(Position.market_id == market_id)
    )).scalar_one())


async def compute_net_shares_held(s, *, mode: str, market_id: str, outcome: str) -> float:
    """Net shares of (market_id, outcome) we currently hold, for ``mode``.

    Live writes only Fill rows (no Position lifecycle), so net = Σ BUY − Σ SELL
    over non-rejected fills (status in filled/submitted/partial). Counting
    SUBMITTED sells (resting / in-flight) as already-reducing is what stops a
    second exit from double-selling the same shares before the first sell fills.
    Paper keeps a Position lifecycle, so its size_shares is authoritative. Exact
    UPPER-cased outcome match — a SELL on "NO" must net only against "NO" BUYs.

    Best-effort: returns 0.0 on any error (fail-safe toward "hold nothing", so we
    can never size a sell against shares we can't prove we hold)."""
    want = (outcome or "").upper()
    if not want:
        return 0.0
    try:
        if mode == "live":
            buy = (await s.execute(
                select(func.coalesce(func.sum(Fill.size_shares), 0.0)).where(
                    Fill.mode == "live",
                    Fill.market_id == market_id,
                    func.upper(Fill.outcome) == want,
                    Fill.side == "BUY",
                    Fill.status.in_(("filled", "submitted", "partial")),
                )
            )).scalar_one()
            sell = (await s.execute(
                select(func.coalesce(func.sum(Fill.size_shares), 0.0)).where(
                    Fill.mode == "live",
                    Fill.market_id == market_id,
                    func.upper(Fill.outcome) == want,
                    Fill.side == "SELL",
                    Fill.status.in_(("filled", "submitted", "partial")),
                )
            )).scalar_one()
            return float(buy or 0.0) - float(sell or 0.0)
        row = (await s.execute(
            select(func.coalesce(func.sum(Position.size_shares), 0.0)).where(
                Position.market_id == market_id,
                func.upper(Position.outcome) == want,
            )
        )).scalar_one()
        return float(row or 0.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("net_shares_query_failed",
                    market_id=market_id, outcome=want, err=str(exc))
        return 0.0


async def preflight(*, mode: str, market_id: str, category: str | None,
                    side: str, size_usdc: float, score: float,
                    outcome: str | None = None, price: float = 0.0,
                    is_exit: bool = False) -> dict:
    """Returns {"ok": True, ...} or raises RiskRejection.

    `mode` (paper|live) is the caller's declared mode (executor's
    settings.trading_mode). We override with the runtime mode from
    Redis so dashboard switches take effect on the very next preflight
    — without restarting the executor. Risk config is also per-mode
    merged so live mode's tighter caps apply when the runtime mode is
    "live".

    `outcome` enables the one-direction-per-market guard (skipped when None,
    e.g. legacy/test callers).

    `is_exit=True` marks a position-CLOSING SELL. preflight re-verifies we
    actually hold the outcome (else it clears the flag and applies the full
    gauntlet — so the flag can't be abused to open a naked short), then skips
    every entry/concentration/cap guard (a close only reduces risk) while keeping
    the order-rate budget. A verified close may also bypass the kill switch,
    gated by exit_mirror.allow_close_when_killed.
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
    exit_cfg = cfg.get("exit_mirror", {})

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

    # Exit re-verification (defence-in-depth). A close must be a SELL that
    # genuinely REDUCES a position we hold. Re-check it here so a caller can't set
    # is_exit=True to skip the entry guards and sneak in a naked short — if we
    # don't actually hold the outcome, clear the flag and run the full gauntlet.
    if is_exit:
        if side != "SELL" or not outcome:
            is_exit = False
        else:
            async with session_scope() as s0:
                net0 = await compute_net_shares_held(
                    s0, mode=order_mode, market_id=market_id, outcome=outcome)
            if net0 <= 0:
                log.warning("exit_flag_cleared_no_position",
                            market_id=market_id, outcome=outcome, net=net0)
                is_exit = False

    # 1) kill switch. A verified close may bypass it — closing only REDUCES risk,
    #    and a tripped breaker is exactly when you want OUT. Gated by
    #    exit_mirror.allow_close_when_killed (default true) so the operator can
    #    still hard-freeze everything. (Order cancels bypass the kill entirely and
    #    never reach preflight.)
    k = await kill_status()
    if k:
        if not is_exit:
            raise RiskRejection(f"kill_switch_active:{k}")
        if not bool(exit_cfg.get("allow_close_when_killed", True)):
            raise RiskRejection(f"kill_switch_active_exit_blocked:{k}")
        log.info("kill_bypass_for_exit", market_id=market_id, kill=str(k))

    # CLOSING fast-path. A verified exit can only reduce risk, so skip EVERY
    # entry/concentration/cap guard below — a SELL is otherwise treated as a NEW
    # opposite-direction bet by direction(), so asset_conflict / crypto_timeframe /
    # factor_cap / per_bet_cap / one_direction_per_market would wrongly block the
    # close. Keep only the order-rate budget so a malfunctioning exit loop can't
    # hammer the venue; spread is advisory on exits (we want out even in a wide book).
    if is_exit:
        async with session_scope() as s:
            recent = (await s.execute(
                select(func.count(Fill.id)).where(
                    Fill.ts >= datetime.now(tz=timezone.utc) - timedelta(seconds=60),
                    Fill.status.in_(("filled", "partial", "submitted")),
                )
            )).scalar_one()
        rate_cap = int(exec_cfg.get("max_orders_per_minute", 6))
        if recent >= rate_cap:
            raise RiskRejection(f"rate_limit_exit:{recent}>={rate_cap}")
        return {"ok": True, "max_size": float(pos_cfg.get("max_position_usdc", 25.0)),
                "is_exit": True}

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

        # One-position-per-CRYPTO-BUCKET: hold at most ONE coherent crypto price bet
        # per asset per UTC resolution day. Daily settlements list many overlapping
        # markets — threshold ("above $X") AND range ("between $A-$B") — and mirroring
        # smart money the bot would otherwise stack contradictory legs, e.g. an open
        # "above 62k NO" while a new order is "between 60-62k NO" for the same day (a
        # self-hedge). Reduces each leg to its win-region and blocks crossing regions;
        # the directional/range guards miss it (separate axes) and the markets often
        # share no populated event_id so the per-event guard misses them too. Keyed on
        # (asset, day, win-region) from the question — event_id-independent — and
        # fail-OPEN on any error. Disable with position.one_position_per_crypto_bucket: false.
        if outcome and pos_cfg.get("one_position_per_crypto_bucket", True):
            try:
                bconf = await _same_day_bucket_conflict(
                    s, mode=order_mode, market_id=market_id,
                    outcome=outcome, side=side)
            except Exception as exc:  # noqa: BLE001
                log.warning("crypto_bucket_conflict_check_failed",
                            market_id=market_id, err=str(exc))
                bconf = None
            if bconf:
                asset, band, held_mid = bconf
                raise RiskRejection(
                    f"crypto_bucket_conflict:{asset}_{band}:held={held_mid[:12]}")

        # Crypto FACTOR exposure cap: treat all crypto majors as ONE bet per
        # direction and bound the GROSS same-direction notional. The per-market,
        # per-asset and per-category caps don't catch this — they bound a single
        # market or net one asset, but nothing limits aggregate directional
        # exposure to the whole correlated complex (BTC/ETH/... move together), so
        # the bot could stack "BTC down" + "BTC below X" + "ETH down" into one
        # outsized bet that halves the account on a single wrong call. Fail-OPEN on
        # any error. null cap (max_crypto_factor_usdc) disables.
        factor_cap = pos_cfg.get("max_crypto_factor_usdc")
        if outcome and factor_cap is not None:
            try:
                fexp = await _crypto_factor_exposure(
                    s, mode=order_mode, market_id=market_id, outcome=outcome,
                    side=side, size_usdc=size_usdc, cap=float(factor_cap))
            except Exception as exc:  # noqa: BLE001
                log.warning("crypto_factor_check_failed",
                            market_id=market_id, err=str(exc))
                fexp = None
            if fexp:
                want_dir, total = fexp
                raise RiskRejection(
                    f"crypto_factor_cap:{want_dir}:{total:.2f}+{size_usdc:.2f}>{factor_cap}")

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

        # 3) per-BET cumulative cap — TOTAL notional committed to this market,
        #    counting resting/in-flight orders (see _bet_notional) so the bot
        #    can't sneak past the ceiling by stacking many small limit orders.
        #    Low-odds bets (entry price < threshold) get a much tighter cap so the
        #    bot can't pour money into long shots: once a sub-threshold bet hits
        #    that small cap, every further order on it is refused.
        existing = await _bet_notional(s, mode=order_mode, market_id=market_id)
        low_odds = 0.0 < price < float(pos_cfg.get("low_odds_price_threshold", 0.20))
        bet_cap = float(
            pos_cfg.get("low_odds_max_per_bet_usdc", 5.0) if low_odds
            else pos_cfg.get("max_per_market_usdc", max_pos)
        )
        if existing + size_usdc > bet_cap:
            tag = ":low_odds" if low_odds else ""
            raise RiskRejection(
                f"per_bet_cap:{existing:.2f}+{size_usdc:.2f}>{bet_cap:.2f}{tag}")

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
