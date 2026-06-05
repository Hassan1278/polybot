"""Paper executor — simulates a fill against the real CLOB orderbook.

We walk the book exactly like a real market order would, charging the spread.
Fees are modelled with the same coefficients Polymarket uses (rounded). The
resulting fill row is indistinguishable from a live fill except `mode=paper`.

Position accounting is long-only (Polymarket binary outcome shares cannot be
shorted): every Position row carries ``size_shares >= 0`` plus a weighted
``avg_price`` and an ever-growing ``realized_pnl_usdc``. SELLs decrement size
and credit realized PnL; full settlement happens via :func:`settle_resolved_markets`
which redeems open paper positions at $1 (winning side) or $0 (losing side)
once ``Market.resolved`` flips true.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.clients import ClobClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.market_resolver import token_for_outcome
from polybot.models import Fill, Market, Position

log = get_logger(__name__)

# ---- knobs ------------------------------------------------------------------

FEE_BPS = 200            # 2.00% taker fee, expressed in basis points
MAKER_FEE = 0.0          # Polymarket currently has no maker fee
TAKER_FEE = FEE_BPS / 10_000.0


@dataclass(frozen=True)
class PaperConfig:
    """Tunable knobs for the paper executor. Kept tiny on purpose — anything
    that affects live trades belongs in risk.yaml, not here."""
    fee_bps: int = FEE_BPS
    # If the bid side of the book is empty when we try to SELL, fall back to
    # midpoint pricing (so simulator can keep running on thin books) instead of
    # outright rejecting.
    sell_fallback_to_midpoint: bool = True
    paper_wallet: str = "PAPER"


CONFIG = PaperConfig()


# ---- helpers ----------------------------------------------------------------


def _walk(levels: list[dict], target_usdc: float) -> tuple[float, float, float] | None:
    """Walk book levels until we've spent ``target_usdc``.

    Returns (shares, notional_filled, avg_price) or None if depth is insufficient.
    """
    shares = 0.0
    notional = 0.0
    for lv in levels:
        p = float(lv["price"]); sz = float(lv["size"])
        cost = p * sz
        if notional + cost >= target_usdc:
            need = (target_usdc - notional) / p
            shares += need
            notional = target_usdc
            return shares, notional, notional / shares
        shares += sz
        notional += cost
    return None


def _walk_shares(levels: list[dict], target_shares: float) -> tuple[float, float, float] | None:
    """Walk levels until we've matched ``target_shares`` (used for SELL/close).

    Returns (shares, notional, avg_price) or None if depth is insufficient.
    """
    shares = 0.0
    notional = 0.0
    for lv in levels:
        p = float(lv["price"]); sz = float(lv["size"])
        if shares + sz >= target_shares:
            need = target_shares - shares
            shares = target_shares
            notional += need * p
            return shares, notional, notional / shares
        shares += sz
        notional += p * sz
    return None


async def _current_position(s, market_id: str, outcome: str) -> Position | None:
    return (await s.execute(
        select(Position).where(and_(
            Position.wallet == CONFIG.paper_wallet,
            Position.market_id == market_id,
            Position.outcome == outcome,
        ))
    )).scalar_one_or_none()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---- public API -------------------------------------------------------------


async def simulate_fill(*, signal_id: int, market_id: str, outcome: str,
                        side: str, size_usdc: float) -> dict:
    """Simulate a market order against the live CLOB book.

    For BUY:  ``size_usdc`` is the dollar notional we want to spend.
    For SELL: ``size_usdc`` is interpreted as the dollar notional we want to
              unwind; we convert it to a share count using the current best bid
              (or midpoint fallback) before walking the book.
    """
    side_u = side.upper()
    async with session_scope() as s:
        m = (await s.execute(
            select(Market.yes_token_id, Market.no_token_id, Market.outcomes)
            .where(Market.market_id == market_id)
        )).first()
        if not m:
            return await _record_reject(signal_id, market_id, outcome, side, "market_unknown")
        # CRITICAL: use centralised token_for_outcome to map outcome string
        # to the correct CLOB token. The old `row[0] if outcome=="YES" else
        # row[1]` pattern silently bought the OPPOSITE side for any non-binary
        # market (e.g. signal said "TYLOO" → executor bought "Lynn Vision").
        # See BUGS.md B14.
        from types import SimpleNamespace
        shim = SimpleNamespace(
            yes_token_id=m[0], no_token_id=m[1], outcomes=m[2], market_id=market_id,
        )
        token_id = token_for_outcome(shim, outcome)
        if not token_id:
            return await _record_reject(signal_id, market_id, outcome, side, "no_token_id")

    c = ClobClient()
    try:
        book = await c.book(token_id)
        # midpoint is only needed for the SELL-empty-book fallback path
        mid = None
        if side_u == "SELL" and not book.get("bids"):
            try:
                mid = await c.midpoint(str(token_id))
            except Exception:  # noqa: BLE001
                mid = None
    finally:
        await c.close()

    raw_levels = book.get("asks") if side_u == "BUY" else book.get("bids")
    if not raw_levels:
        if side_u == "SELL" and CONFIG.sell_fallback_to_midpoint and mid and mid > 0:
            return await _execute_sell_at_price(
                signal_id=signal_id, market_id=market_id, outcome=outcome,
                size_usdc=size_usdc, fill_price=float(mid), source="midpoint",
            )
        reason = "no_bids" if side_u == "SELL" else "no_levels"
        return await _record_reject(signal_id, market_id, outcome, side, reason)

    # BUY: asks ascending (cheapest first). SELL: bids descending (highest first).
    levels = sorted([{"price": l["price"], "size": l["size"]} for l in raw_levels],
                    key=lambda l: float(l["price"]), reverse=(side_u == "SELL"))

    if side_u == "BUY":
        walked = _walk(levels, size_usdc)
        if not walked:
            return await _record_reject(signal_id, market_id, outcome, side, "insufficient_depth")
        shares, notional, avg = walked
    else:
        # Convert dollar notional → target share count using best bid.
        best_bid = float(levels[0]["price"])
        if best_bid <= 0:
            return await _record_reject(signal_id, market_id, outcome, side, "no_bids")
        target_shares = size_usdc / best_bid
        walked = _walk_shares(levels, target_shares)
        if not walked:
            return await _record_reject(signal_id, market_id, outcome, side, "insufficient_depth")
        shares, notional, avg = walked

    return await _persist_fill(
        signal_id=signal_id, market_id=market_id, outcome=outcome,
        side=side_u, shares=shares, fill_price=avg, notional=notional,
    )


async def close_position(market_id: str, outcome: str, fraction: float = 1.0) -> dict:
    """SELL ``fraction`` (0 < fraction <= 1) of the current PAPER position
    into the live book. Returns the resulting fill record (or rejection)."""
    if not (0.0 < fraction <= 1.0):
        return await _record_reject(None, market_id, outcome, "SELL", f"bad_fraction:{fraction}")

    async with session_scope() as s:
        pos = await _current_position(s, market_id, outcome)
        if not pos or pos.size_shares <= 0:
            return await _record_reject(None, market_id, outcome, "SELL", "no_position")
        target_shares = pos.size_shares * fraction
        m = (await s.execute(
            select(Market.yes_token_id, Market.no_token_id, Market.outcomes)
            .where(Market.market_id == market_id)
        )).first()
        if not m:
            return await _record_reject(None, market_id, outcome, "SELL", "market_unknown")
        from types import SimpleNamespace
        shim = SimpleNamespace(
            yes_token_id=m[0], no_token_id=m[1], outcomes=m[2], market_id=market_id,
        )
        token_id = token_for_outcome(shim, outcome)
        if not token_id:
            return await _record_reject(None, market_id, outcome, "SELL", "no_token_id")

    c = ClobClient()
    try:
        book = await c.book(token_id)
        mid = None
        if not book.get("bids"):
            try:
                mid = await c.midpoint(str(token_id))
            except Exception:  # noqa: BLE001
                mid = None
    finally:
        await c.close()

    bids = book.get("bids")
    if not bids:
        if CONFIG.sell_fallback_to_midpoint and mid and mid > 0:
            notional = target_shares * float(mid)
            return await _execute_sell_at_price(
                signal_id=None, market_id=market_id, outcome=outcome,
                size_usdc=notional, fill_price=float(mid), source="midpoint",
            )
        return await _record_reject(None, market_id, outcome, "SELL", "no_bids")

    levels = sorted([{"price": b["price"], "size": b["size"]} for b in bids],
                    key=lambda l: float(l["price"]), reverse=True)
    walked = _walk_shares(levels, target_shares)
    if not walked:
        return await _record_reject(None, market_id, outcome, "SELL", "insufficient_depth")
    shares, notional, avg = walked
    return await _persist_fill(
        signal_id=None, market_id=market_id, outcome=outcome,
        side="SELL", shares=shares, fill_price=avg, notional=notional,
    )


async def settle_resolved_markets() -> list[dict]:
    """Realize PnL for every open PAPER position whose market has resolved.

    This routine should be called periodically by ``pnl_loop`` (e.g. once per
    minute alongside the equity snapshot). Idempotent: settled positions have
    ``size_shares=0`` and are skipped on subsequent runs.

    Settlement price is $1 if our outcome matches ``Market.outcome``, else $0.
    Realized PnL credited = (settled_price - avg_price) * size. Fees on the
    underlying BUYs were already deducted at fill time, so we do not double-count
    them here.
    """
    settled: list[dict] = []
    async with session_scope() as s:
        rows = (await s.execute(
            select(Position, Market.outcome)
            .join(Market, Market.market_id == Position.market_id)
            .where(and_(
                Position.wallet == CONFIG.paper_wallet,
                Position.size_shares > 0,
                Market.resolved.is_(True),
            ))
        )).all()

        for pos, market_outcome in rows:
            settled_price = 1.0 if (market_outcome and
                                    market_outcome.upper() == pos.outcome.upper()) else 0.0
            size = float(pos.size_shares)
            avg = float(pos.avg_price)
            realized_delta = (settled_price - avg) * size  # fees already booked at entry
            now = _now()

            s.add(Fill(
                signal_id=None,
                ts=now,
                mode="paper",
                market_id=pos.market_id,
                outcome=pos.outcome,
                side="SETTLE",
                size_shares=size,
                price=settled_price,
                notional_usdc=settled_price * size,
                fee_usdc=0.0,
                status="settled",
                venue_order_id=None,
            ))
            await s.execute(
                update(Position)
                .where(Position.id == pos.id)
                .values(
                    size_shares=0.0,
                    realized_pnl_usdc=Position.realized_pnl_usdc + realized_delta,
                    updated_at=now,
                )
            )
            settled.append({
                "market_id": pos.market_id, "outcome": pos.outcome,
                "settled_price": settled_price, "shares": size,
                "realized_delta": realized_delta,
            })
            log.info("paper_settle", market=pos.market_id, outcome=pos.outcome,
                     settled_price=settled_price, shares=round(size, 2),
                     realized_delta=round(realized_delta, 4))
    return settled


# ---- internal: persistence --------------------------------------------------


async def _execute_sell_at_price(*, signal_id: int | None, market_id: str,
                                 outcome: str, size_usdc: float,
                                 fill_price: float, source: str) -> dict:
    """SELL fallback path when the book has no bids — price the unwind at
    ``fill_price`` (typically the midpoint)."""
    if fill_price <= 0:
        return await _record_reject(signal_id, market_id, outcome, "SELL", "no_bids")
    shares = size_usdc / fill_price
    notional = shares * fill_price
    log.info("paper_sell_fallback", market=market_id, source=source, price=fill_price)
    return await _persist_fill(
        signal_id=signal_id, market_id=market_id, outcome=outcome,
        side="SELL", shares=shares, fill_price=fill_price, notional=notional,
    )


async def _persist_fill(*, signal_id: int | None, market_id: str, outcome: str,
                        side: str, shares: float, fill_price: float,
                        notional: float) -> dict:
    """Write the Fill row and weighted-average / realize the Position row.

    BUY:  new_size = old + delta;
          new_avg  = (old_size*old_avg + delta*fill_price) / new_size
    SELL: realized_delta = (fill_price - old_avg) * sold_shares - fees;
          new_size = max(0, old_size - sold_shares); avg unchanged.
    """
    fee = notional * (CONFIG.fee_bps / 10_000.0)
    now = _now()
    realized_delta = 0.0

    async with session_scope() as s:
        pos = await _current_position(s, market_id, outcome)

        if side == "BUY":
            old_size = float(pos.size_shares) if pos else 0.0
            old_avg = float(pos.avg_price) if pos else 0.0
            new_size = old_size + shares
            new_avg = ((old_size * old_avg) + (shares * fill_price)) / new_size if new_size > 0 else 0.0
            # BUY fees are realized losses against the cost basis (kept here so
            # the PnL ledger reflects total trading cost).
            realized_delta = -fee
        elif side == "SELL":
            if not pos or pos.size_shares <= 0:
                # Persist a rejected fill so the audit trail is complete.
                return await _record_reject(signal_id, market_id, outcome, side, "no_position")
            sold = min(float(shares), float(pos.size_shares))
            old_size = float(pos.size_shares)
            old_avg = float(pos.avg_price)
            realized_delta = (fill_price - old_avg) * sold - fee
            new_size = max(0.0, old_size - sold)
            new_avg = old_avg  # avg cost basis unchanged on partial close
            # Re-stamp the fill's share count to what we actually unwound.
            shares = sold
            notional = sold * fill_price
        else:
            return await _record_reject(signal_id, market_id, outcome, side, f"unknown_side:{side}")

        s.add(Fill(
            signal_id=signal_id,
            ts=now,
            mode="paper",
            market_id=market_id,
            outcome=outcome,
            side=side,
            size_shares=shares,                # always >= 0
            price=fill_price,
            notional_usdc=notional,
            fee_usdc=fee,
            status="filled",
            venue_order_id=None,
        ))

        await s.execute(
            pg_insert(Position).values(
                wallet=CONFIG.paper_wallet,
                market_id=market_id,
                outcome=outcome,
                size_shares=new_size,
                avg_price=new_avg,
                realized_pnl_usdc=realized_delta,
                updated_at=now,
            ).on_conflict_do_update(
                index_elements=["wallet", "market_id", "outcome"],
                set_={
                    "size_shares": new_size,
                    "avg_price": new_avg,
                    "realized_pnl_usdc": Position.realized_pnl_usdc + realized_delta,
                    "updated_at": now,
                },
            )
        )

    log.info("paper_fill", signal=signal_id, market=market_id, side=side,
             shares=round(shares, 2), price=round(fill_price, 4),
             notional=round(notional, 2), realized_delta=round(realized_delta, 4))
    return {
        "status": "filled", "shares": shares, "avg_price": fill_price,
        "notional": notional, "fee": fee, "realized_delta": realized_delta,
    }


async def _record_reject(signal_id: int | None, market_id: str, outcome: str,
                         side: str, reason: str) -> dict:
    async with session_scope() as s:
        s.add(Fill(
            signal_id=signal_id, ts=_now(),
            mode="paper", market_id=market_id, outcome=outcome,
            side=side, size_shares=0.0, price=0.0,
            notional_usdc=0.0, fee_usdc=0.0,
            status="rejected", error=reason,
        ))
    log.warning("paper_reject", reason=reason, signal=signal_id, market=market_id)
    return {"status": "rejected", "reason": reason}
