"""Live executor — places real orders via py-clob-client-v2.

Mirrors the paper executor's interface exactly so the signal-consumer in
`main.py` is one line different ("paper" vs "live").

By default we send GTC *maker* limits priced one tick BEHIND the best
opposite quote — so we don't cross the spread and don't pay taker fees.
For the rare case where the engine needs to cross (urgent close, kill, etc.)
pass `order_kind="taker"` and we'll quote one tick INSIDE the opposite top
and submit IOC.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from polybot.clients import ClobClient, DataClient
from polybot.config import settings
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Fill, Market

log = get_logger(__name__)

TICK = 0.001
MIN_PX = 0.001
MAX_PX = 0.999
# Polymarket V2 rejects orders below 5 shares ("Size (x) lower than the
# minimum: 5"). Floor every live order at this so small-dollar signals still
# place instead of being bounced before they can rest. The bumped notional is
# at most ~5 * price ≈ a few dollars, well under max_position_usdc.
MIN_SHARES = 5.0


def _round_to_tick(px: float, tick: float = TICK) -> float:
    """Round to the nearest tick and clamp into the legal [0.001, 0.999] range."""
    snapped = round(px / tick) * tick
    return max(MIN_PX, min(MAX_PX, round(snapped, 4)))


def _best(levels: list[dict] | None, *, highest: bool) -> float | None:
    """Return the best price from one side of the book, or None if empty.

    `highest=True` for bids (we want the max), `highest=False` for asks (min).
    """
    if not levels:
        return None
    prices = [float(l["price"]) for l in levels if "price" in l]
    if not prices:
        return None
    return max(prices) if highest else min(prices)


def _maker_price(book: dict, side: str, tick: float = TICK) -> float:
    """Passive maker price — sits on our OWN side of the book, never crosses.

    - BUY  maker: best_bid + tick  (improve the bid by one tick; still below best_ask)
    - SELL maker: best_ask - tick  (improve the ask by one tick; still above best_bid)

    If improving by a tick would cross the spread (1-tick wide book), we fall
    back to joining the existing top quote on our side.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = _best(bids, highest=True)
    best_ask = _best(asks, highest=False)

    if side.upper() == "BUY":
        if best_bid is None and best_ask is None:
            px = 0.5
        elif best_bid is None:
            px = max(MIN_PX, (best_ask or MAX_PX) - tick)
        else:
            px = best_bid + tick
            # Don't cross: if improving the bid would meet/exceed best_ask,
            # just join the existing best bid.
            if best_ask is not None and px >= best_ask:
                px = best_bid
    else:  # SELL
        if best_ask is None and best_bid is None:
            px = 0.5
        elif best_ask is None:
            px = min(MAX_PX, (best_bid or MIN_PX) + tick)
        else:
            px = best_ask - tick
            if best_bid is not None and px <= best_bid:
                px = best_ask

    return _round_to_tick(px, tick)


def _taker_price(book: dict, side: str, tick: float = TICK) -> float:
    """Aggressive taker price — quotes one tick INSIDE the opposite top so the
    order is virtually guaranteed to cross. Pair with IOC at the call site.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if side.upper() == "BUY":
        best_ask = _best(asks, highest=False)
        if best_ask is None:
            best_bid = _best(bids, highest=True)
            px = (best_bid + tick) if best_bid is not None else 0.5
        else:
            px = best_ask + tick
    else:  # SELL
        best_bid = _best(bids, highest=True)
        if best_bid is None:
            best_ask = _best(asks, highest=False)
            px = (best_ask - tick) if best_ask is not None else 0.5
        else:
            px = best_bid - tick
    return _round_to_tick(px, tick)


def _allowance_hint() -> None:
    """We can't cheaply verify the USDC.e -> CTF Exchange allowance from here
    without an RPC call, so just print a one-shot hint so live-mode operators
    don't burn signals on signing-failure rejections.
    """
    if not getattr(_allowance_hint, "_warned", False):
        log.warning(
            "live_allowance_hint",
            msg=("FUNDER must approve USDC.e (0x2791Bca1...) AND the CTF "
                 "(ERC1155, 0x4D97DCd9...) to ALL THREE Polymarket spenders: "
                 "the CTF Exchange, the Neg-Risk CTF Exchange, and the "
                 "Neg-Risk Adapter. This bot trades multi-outcome markets "
                 "(sports/elections) which settle through the Neg-Risk "
                 "contracts — approving only the plain CTF Exchange leaves "
                 "every multi-outcome order rejected with 'not enough "
                 "allowance'. Set the approvals once from the FUNDER wallet."),
            funder=getattr(settings, "polymarket_funder_address", None),
        )
        _allowance_hint._warned = True  # type: ignore[attr-defined]


async def _place_order(*, signal_id: int | None, market_id: str, outcome: str,
                       side: str, order_kind: str = "maker",
                       size_usdc: float | None = None,
                       shares: float | None = None) -> dict:
    """Core order placement shared by entries and exits: resolve token → fetch
    book → price → submit → record. Pass EXACTLY one of:

      - ``size_usdc`` (entries): converted to shares at the quoted price and
        floored UP to the venue's MIN_SHARES so small-dollar signals still place.
      - ``shares``    (exits):   used as-is. A sub-minimum count is REJECTED, never
        floored up — flooring an exit up would oversell into a naked short.
    """
    kind = (order_kind or "maker").lower()
    if kind not in ("maker", "taker"):
        return await _record(signal_id, market_id, outcome, side, "rejected",
                             reason=f"bad_order_kind:{order_kind}")

    async with session_scope() as s:
        m = (await s.execute(
            select(Market.yes_token_id, Market.no_token_id, Market.outcomes)
            .where(Market.market_id == market_id)
        )).first()
        if not m:
            return await _record(signal_id, market_id, outcome, side, "rejected",
                                 reason="market_unknown")
        # See BUGS.md B14 — non-binary outcomes (sport teams, candidates)
        # MUST go through token_for_outcome or the executor bets the wrong
        # side. The legacy `row[0] if outcome=="YES" else row[1]` pattern
        # systematically inverted intent on non-binary markets.
        from polybot.market_resolver import token_for_outcome
        from types import SimpleNamespace
        shim = SimpleNamespace(
            yes_token_id=m[0], no_token_id=m[1], outcomes=m[2], market_id=market_id,
        )
        token_id = token_for_outcome(shim, outcome)
        if not token_id:
            return await _record(signal_id, market_id, outcome, side, "rejected",
                                 reason="token_id_missing")

    _allowance_hint()

    c = ClobClient()
    try:
        book = await c.book(token_id)
        if not (book.get("bids") or book.get("asks")):
            return await _record(signal_id, market_id, outcome, side, "rejected", reason="no_book")

        if kind == "maker":
            px = _maker_price(book, side)
            order_type = "GTC"
        else:
            px = _taker_price(book, side)
            order_type = "IOC"

        if not (MIN_PX <= px <= MAX_PX):
            return await _record(signal_id, market_id, outcome, side, "rejected",
                                 reason=f"price_out_of_range:{px}")

        if shares is None:
            # Entry: dollar-sized at the quote. Enforce the venue's 5-share floor
            # (see MIN_SHARES): below it the CLOB rejects "Size lower than the
            # minimum: 5", so bump up and keep the recorded notional consistent.
            order_shares = size_usdc / px
            if order_shares < MIN_SHARES:
                order_shares = MIN_SHARES
        else:
            # Exit / explicit shares: use as-is — NEVER floor up (that would
            # oversell into a naked short). The venue rejects < MIN_SHARES, so we
            # refuse dust cleanly rather than round it away.
            order_shares = shares
            if order_shares < MIN_SHARES:
                return await _record(signal_id, market_id, outcome, side, "rejected",
                                     reason=f"shares_below_min:{order_shares:.4f}<{MIN_SHARES}")
        notional = round(order_shares * px, 6)

        try:
            resp = await c.place_limit(token_id=token_id, side=side.upper(),
                                       price=px, size=order_shares, order_type=order_type)
        except RuntimeError as exc:
            # ClobClient raises RuntimeError for signing/credential problems.
            msg = str(exc).lower()
            if "can_sign" in msg or "private_key" in msg or "funder" in msg:
                return await _record(signal_id, market_id, outcome, side, "rejected",
                                     reason=f"signing_not_configured:{exc}")
            if "py-clob-client" in msg:
                return await _record(signal_id, market_id, outcome, side, "rejected",
                                     reason=f"sdk_missing:{exc}")
            raise
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if "insufficient" in text and ("usdc" in text or "balance" in text or "fund" in text):
                log.warning("live_insufficient_usdc", err=str(exc))
                return await _record(signal_id, market_id, outcome, side, "rejected",
                                     reason=f"insufficient_usdc:{exc}")
            if "allowance" in text or "approval" in text:
                log.warning("live_allowance_missing", err=str(exc))
                return await _record(signal_id, market_id, outcome, side, "rejected",
                                     reason=f"allowance_missing:{exc}")
            if "sign" in text or "nonce" in text or "eip712" in text:
                log.warning("live_signing_error", err=str(exc))
                return await _record(signal_id, market_id, outcome, side, "rejected",
                                     reason=f"signing_error:{exc}")
            raise
    except Exception as exc:  # noqa: BLE001
        log.exception("live_place_failed")
        return await _record(signal_id, market_id, outcome, side, "rejected", reason=str(exc))
    finally:
        await c.close()

    status = (resp.get("status") or "submitted").lower()
    venue_id = resp.get("orderID") or resp.get("orderId")
    return await _record(signal_id, market_id, outcome, side, status,
                         shares=order_shares, price=px, notional=notional,
                         venue_order_id=venue_id, raw=resp)


async def place_live(*, signal_id: int, market_id: str, outcome: str,
                     side: str, size_usdc: float,
                     order_kind: str = "maker") -> dict:
    """Place a real ENTRY order, dollar-sized. `order_kind` 'maker' (GTC) / 'taker' (IOC)."""
    return await _place_order(signal_id=signal_id, market_id=market_id, outcome=outcome,
                              side=side, order_kind=order_kind, size_usdc=size_usdc)


async def place_live_shares(*, signal_id: int | None, market_id: str, outcome: str,
                            side: str, shares: float,
                            order_kind: str = "taker") -> dict:
    """Place a real order sized by an explicit SHARE count (exits). Never floors up
    to the venue minimum — the caller must pass a count it has CONFIRMED is held.
    Defaults to taker (IOC) so a close crosses the book and actually fills."""
    return await _place_order(signal_id=signal_id, market_id=market_id, outcome=outcome,
                              side=side, order_kind=order_kind, shares=shares)


async def live_shares_held(market_id: str, outcome: str) -> float | None:
    """Authoritative shares held of (market_id, outcome) on the venue, read from
    the data-api positions endpoint (same source as /live/account).

    This — NOT the local Fill ledger — is what sizes a live exit. The live path is
    fire-and-forget: a 'submitted' Fill is never reconciled to filled/cancelled,
    so summing Fills can't tell resting from filled and would risk overselling
    into a naked short. Returns None on ANY failure (no wallet, token unresolved,
    data-api error) so the caller fails safe and does NOT sell blind; returns 0.0
    when the wallet simply holds none of this outcome.
    """
    from services.executor.equity_guard import _deposit_wallet
    funder = await _deposit_wallet()
    if not funder:
        return None
    async with session_scope() as s:
        m = (await s.execute(
            select(Market.yes_token_id, Market.no_token_id, Market.outcomes)
            .where(Market.market_id == market_id)
        )).first()
    if not m:
        return None
    from polybot.market_resolver import token_for_outcome
    from types import SimpleNamespace
    shim = SimpleNamespace(yes_token_id=m[0], no_token_id=m[1], outcomes=m[2],
                           market_id=market_id)
    token_id = token_for_outcome(shim, outcome)
    if not token_id:
        return None
    d = DataClient()
    try:
        rows = await d.positions(funder, limit=500, size_threshold=0.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("live_shares_held_failed", market_id=market_id, err=str(exc))
        return None
    finally:
        await d.close()
    for p in (rows or []):
        if isinstance(p, dict) and str(p.get("asset") or "") == str(token_id):
            try:
                return float(p.get("size") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0          # wallet holds no position in this token


async def _record(signal_id: int, market_id: str, outcome: str, side: str,
                  status: str, *, shares: float = 0.0, price: float = 0.0,
                  notional: float = 0.0, venue_order_id: str | None = None,
                  reason: str | None = None, raw: dict | None = None) -> dict:
    async with session_scope() as s:
        s.add(Fill(
            signal_id=signal_id,
            ts=datetime.now(tz=timezone.utc),
            mode="live", market_id=market_id, outcome=outcome,
            side=side, size_shares=shares, price=price,
            notional_usdc=notional, fee_usdc=0.0,
            status=status, venue_order_id=venue_order_id, error=reason,
        ))
    log.info("live_order_result", signal=signal_id, status=status, market=market_id,
             venue=venue_order_id, reason=reason)
    return {"status": status, "venue_order_id": venue_order_id, "raw": raw}
