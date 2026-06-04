"""Live CLOB WebSocket listener — WS-driven attribution via REST burst-poll.

Why this exists
---------------
The naive design ("subscribe to /ws/market and filter messages by
maker/taker") cannot work: the public market channel emits an anonymized
trade tape (``last_trade_price``) that contains only price/size/side/asset
— no addresses, no transactionHash.  Wallet-attributed fills are only
available on the L2-authenticated ``/ws/user`` channel for wallets we
*own the API keys for*, which is not the case for tracked third-party
wallets.

So we use the WS as a **smart wakeup signal**:

    WS sees a burst of trade prints on market X
        -> trigger an immediate REST poll of /data-api/trades?market=X
        -> any returned trade whose proxyWallet is in our tracked set
           is upserted into the ``trades`` table with source='ws'
           and announced on Redis (``trade:new``)

The data we *do* get from the market channel — book / price_change /
tick_size_change / best_bid_ask — is logged in a counter so the strategy
layer can later subscribe to the same socket via a Redis fanout instead
of opening duplicate sockets.

Outer loop refreshes the asset-id and tracked-wallet sets every 10 min so
newly-discovered markets/wallets join without a service restart.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter, defaultdict, deque
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from polybot.clients import DataClient
from polybot.clients.ws import ClobWebSocket
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market, Trade, Wallet
from polybot.redis_bus import publish

log = get_logger(__name__)


# ---- burst-detection tuning ----------------------------------------------
BURST_WINDOW_SEC = 30.0       # rolling window per market
BURST_THRESHOLD = 3            # >= this many prints in window triggers a poll
POLL_COOLDOWN_SEC = 20.0       # don't re-poll same market more often than this
ATTRIBUTION_LOOKBACK_SEC = 120 # only attribute trades younger than this
MAX_WS_ASSET_IDS = 500         # per-connection subscription cap (documented)
METRICS_INTERVAL_SEC = 60.0    # ops-log dispatch counters this often
SUBSCRIPTION_REFRESH_SEC = 600 # 10 min — matches previous behavior


# ---- DB helpers ----------------------------------------------------------


async def _tracked_wallet_set() -> set[str]:
    async with session_scope() as s:
        rows = (await s.execute(select(Wallet.address).where(Wallet.is_active.is_(True)))).all()
    return {r[0].lower() for r in rows}


async def _active_markets() -> tuple[list[str], dict[str, tuple[str, str]]]:
    """Return (asset_ids, token_id -> (market_id, outcome)).

    The reverse map lets us resolve which condition_id (market) a given
    asset_id belongs to without a round-trip to the DB for every WS
    message.
    """
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Market.market_id, Market.yes_token_id, Market.no_token_id).where(
                    Market.resolved.is_(False)
                )
            )
        ).all()
    asset_ids: list[str] = []
    by_token: dict[str, tuple[str, str]] = {}
    for market_id, y, n in rows:
        if y:
            tid = str(y)
            asset_ids.append(tid)
            by_token[tid] = (market_id, "YES")
        if n:
            tid = str(n)
            asset_ids.append(tid)
            by_token[tid] = (market_id, "NO")
    return asset_ids, by_token


# ---- main entrypoint -----------------------------------------------------


async def run_live_listener() -> None:
    """Refresh subscriptions every ~10 min; run a consumer in between."""
    while True:
        try:
            asset_ids, token_index = await _active_markets()
            tracked = await _tracked_wallet_set()
            if not asset_ids:
                log.info("live_listener_no_assets_sleeping")
                await asyncio.sleep(60)
                continue
            if len(asset_ids) > MAX_WS_ASSET_IDS:
                log.warning(
                    "live_listener_asset_cap_truncated",
                    total=len(asset_ids),
                    capped_at=MAX_WS_ASSET_IDS,
                )
                asset_ids = asset_ids[:MAX_WS_ASSET_IDS]

            log.info(
                "live_listener_subscribing",
                assets=len(asset_ids),
                tracked_wallets=len(tracked),
                markets=len(set(v[0] for v in token_index.values())),
            )

            ws = ClobWebSocket(asset_ids=asset_ids)
            data_client = DataClient()
            consume_task = asyncio.create_task(
                _consume(ws, tracked=tracked, token_index=token_index, data_client=data_client)
            )
            metrics_task = asyncio.create_task(_metrics_loop(ws))
            try:
                await asyncio.sleep(SUBSCRIPTION_REFRESH_SEC)
            finally:
                ws.stop()
                consume_task.cancel()
                metrics_task.cancel()
                for t in (consume_task, metrics_task):
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                await data_client.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("live_listener_outer_failed")
            await asyncio.sleep(30)


# ---- consumer ------------------------------------------------------------


async def _consume(
    ws: ClobWebSocket,
    *,
    tracked: set[str],
    token_index: dict[str, tuple[str, str]],
    data_client: DataClient,
) -> None:
    """Read events off the WS, dispatch by type, trigger bursts -> polls."""
    # Per-market rolling window of trade-print timestamps and last poll time.
    print_window: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=64))
    last_poll: dict[str, float] = {}
    # Avoid double-inserting the same transactionHash we attributed in the
    # last burst poll; the DB has a partial unique index but a local cache
    # avoids the wasted insert+conflict round-trip.
    seen_tx: deque[str] = deque(maxlen=2048)
    seen_tx_set: set[str] = set()

    async for evt in ws.stream():
        et = evt.get("event_type")
        payload = evt.get("raw") or {}

        if et == "last_trade_price":
            await _on_last_trade_price(
                payload,
                token_index=token_index,
                tracked=tracked,
                data_client=data_client,
                print_window=print_window,
                last_poll=last_poll,
                seen_tx=seen_tx,
                seen_tx_set=seen_tx_set,
            )
        elif et in {"book", "price_change", "tick_size_change", "best_bid_ask"}:
            # Pure market-data signals — interesting to the strategy layer
            # but we don't persist them here.  A future Redis fanout can
            # carry them downstream.
            pass
        elif et in {"new_market", "market_resolved"}:
            log.info("ws_market_lifecycle", event_type=et, payload_keys=list(payload.keys()))
        else:
            # Unknown event already logged once by ws.py; nothing else to do.
            pass


async def _on_last_trade_price(
    payload: dict[str, Any],
    *,
    token_index: dict[str, tuple[str, str]],
    tracked: set[str],
    data_client: DataClient,
    print_window: dict[str, deque[float]],
    last_poll: dict[str, float],
    seen_tx: deque[str],
    seen_tx_set: set[str],
) -> None:
    """Burst-detect on a trade print and trigger a REST attribution poll."""
    asset_id = str(payload.get("asset_id") or "")
    if not asset_id:
        return
    resolved = token_index.get(asset_id)
    if not resolved:
        # Trade print on an asset we no longer track (resolved market, or
        # arrived between two refresh cycles).  Skip without noise.
        return
    market_id, _outcome = resolved

    now = time.monotonic()
    window = print_window[market_id]
    window.append(now)
    # Drop timestamps outside the rolling window.
    cutoff = now - BURST_WINDOW_SEC
    while window and window[0] < cutoff:
        window.popleft()

    if len(window) < BURST_THRESHOLD:
        return
    if now - last_poll.get(market_id, 0.0) < POLL_COOLDOWN_SEC:
        return

    last_poll[market_id] = now
    log.info(
        "ws_price_burst",
        market_id=market_id,
        prints_in_window=len(window),
        window_sec=BURST_WINDOW_SEC,
    )

    try:
        rows = await data_client.market_trades(market_id, limit=200)
    except Exception as exc:  # noqa: BLE001
        log.warning("ws_burst_poll_failed", market_id=market_id, err=str(exc))
        return

    await _attribute_and_persist(
        rows,
        tracked=tracked,
        seen_tx=seen_tx,
        seen_tx_set=seen_tx_set,
    )


# ---- attribution + persistence -------------------------------------------


def _row_wallet(row: dict[str, Any]) -> str | None:
    """Pull the proxy wallet out of a /data-api/trades row.

    The endpoint uses ``proxyWallet`` for the trader address; older payloads
    occasionally use ``user`` or ``maker``/``taker``.  Normalize to lower.
    """
    for key in ("proxyWallet", "proxy_wallet", "user", "owner", "maker", "taker"):
        v = row.get(key)
        if isinstance(v, str) and v.startswith("0x"):
            return v.lower()
    return None


def _row_ts(row: dict[str, Any]) -> datetime | None:
    raw = row.get("timestamp") or row.get("time")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


async def _attribute_and_persist(
    rows: Iterable[dict[str, Any]],
    *,
    tracked: set[str],
    seen_tx: deque[str],
    seen_tx_set: set[str],
) -> None:
    """For each REST trade row, persist iff proxyWallet is tracked + recent."""
    now_ts = datetime.now(tz=timezone.utc)
    written = 0
    async with session_scope() as s:
        for row in rows:
            wallet = _row_wallet(row)
            if not wallet or wallet not in tracked:
                continue
            ts = _row_ts(row) or now_ts
            if (now_ts - ts).total_seconds() > ATTRIBUTION_LOOKBACK_SEC:
                # Older trades will be picked up by the periodic
                # trade_ingest backfill; we only care about hot ones here.
                continue
            tx = row.get("transactionHash") or row.get("tx_hash")
            if tx and tx in seen_tx_set:
                continue

            market_id = row.get("conditionId") or row.get("market") or ""
            if not market_id:
                continue
            size = float(row.get("size", 0) or 0)
            price = float(row.get("price", 0) or 0)
            side = (row.get("side") or "BUY").upper()
            outcome = (row.get("outcome") or "YES").upper()

            stmt = pg_insert(Trade).values(
                tx_hash=tx,
                ts=ts,
                wallet=wallet,
                market_id=market_id,
                outcome=outcome,
                side=side,
                size_shares=size,
                price=price,
                notional_usdc=size * price,
                fee_usdc=float(row.get("fee", 0) or 0),
                source="ws",
            )
            if tx:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["tx_hash"],
                    index_where=text("tx_hash IS NOT NULL"),
                )
            await s.execute(stmt)
            written += 1

            if tx:
                seen_tx.append(tx)
                seen_tx_set.add(tx)
                # Trim the set to match the bounded deque.
                if len(seen_tx_set) > seen_tx.maxlen:
                    # rebuild from the deque to drop evicted entries
                    seen_tx_set.clear()
                    seen_tx_set.update(seen_tx)

            await publish(
                "trade:new",
                {
                    "wallet": wallet,
                    "market_id": market_id,
                    "outcome": outcome,
                    "side": side,
                    "size": size,
                    "price": price,
                    "ts": int(ts.timestamp()),
                    "source": "ws",
                },
            )

    if written:
        log.info("ws_burst_attributed", trades=written)


# ---- ops metrics ---------------------------------------------------------


async def _metrics_loop(ws: ClobWebSocket) -> None:
    """Emit a Prometheus-style counter log of WS dispatches every ~60s.

    The counter lives on the WS client and accumulates across reconnects
    until the outer loop replaces the client at refresh time.
    """
    prev: Counter[str] = Counter()
    while True:
        await asyncio.sleep(METRICS_INTERVAL_SEC)
        cur = Counter(ws.counters)
        delta = cur - prev  # only positive diffs
        prev = cur
        if delta:
            for evt, n in sorted(delta.items()):
                log.info("ws_message_count", event_type=evt, n=n)
