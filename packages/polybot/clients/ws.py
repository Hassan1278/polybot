"""Polymarket CLOB WebSocket client.

Two distinct channels are exposed:

* ``/ws/market`` — public, no auth. Pushes orderbook + anonymized trade tape
  for a list of ERC1155 ``asset_ids``.  Event types we surface:

    - ``book``              — full L2 snapshot
    - ``price_change``      — incremental top-of-book delta(s)
    - ``tick_size_change``  — min-tick promotion/demotion
    - ``last_trade_price``  — anonymized trade print (NO maker/taker/tx_hash)
    - ``best_bid_ask``      — top-of-book ticker (requires ``custom_feature_enabled``)
    - ``new_market``        — market creation (requires ``custom_feature_enabled``)
    - ``market_resolved``   — market settlement (requires ``custom_feature_enabled``)

* ``/ws/user`` — L2-authenticated. The ONLY channel that emits wallet-
  attributed fills (``owner``, ``trade_owner``, ``maker_orders[].owner``).
  We keep optional hooks here so a future wallet-custody integration can
  plug in without rewriting the consumer.  Event types:

    - ``trade``  — fill lifecycle (status MATCHED→MINED→CONFIRMED)
    - ``order``  — order lifecycle (PLACEMENT/UPDATE/CANCELLATION)

Critical wire-protocol details validated against the public docs and the
real-time-data-client / clob-client-v2 sources:

1. The server pushes a **JSON array** of events per frame (batched).  We
   iterate and yield each element individually so downstream code can stay
   schema-flat.
2. Polymarket emits an application-level text frame ``"PING"`` every ~5s
   and disconnects after ~10s without a reply.  The ``websockets`` library
   only auto-handles RFC6455 control-frame pings, so we additionally reply
   ``"PONG"`` to any short text frame containing ``PING``.
3. ``custom_feature_enabled: true`` unlocks ``best_bid_ask`` / ``new_market`` /
   ``market_resolved`` events on the market channel — cheap to set, useful
   downstream, so it is on by default.
4. Reconnect uses capped exponential backoff and resubscribes from scratch
   (auth is re-sent each time for the user channel).
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import websockets

from polybot.config import settings
from polybot.logging import get_logger

log = get_logger(__name__)


# Event types we know how to dispatch.  Anything else is yielded with
# ``channel`` set but logged as ``unknown`` once per type so we notice new
# message kinds without spamming.
MARKET_EVENTS = frozenset(
    {
        "book",
        "price_change",
        "tick_size_change",
        "last_trade_price",
        "best_bid_ask",
        "new_market",
        "market_resolved",
    }
)
USER_EVENTS = frozenset({"trade", "order"})


def _ws_base() -> str:
    """Return the WS base URL without a trailing slash.

    The configured base may or may not already include ``/ws``; the docs
    URL is ``wss://ws-subscriptions-clob.polymarket.com/ws``.  We normalize
    to always end with ``/ws`` so the channel suffix yields ``/ws/market``
    or ``/ws/user`` regardless of how the env was set.
    """
    base = settings.polymarket_ws_url.rstrip("/")
    if not base.endswith("/ws"):
        base = base + "/ws"
    return base


class ClobWebSocket:
    """Subscribe to the public ``/ws/market`` channel.

    Yields normalized dicts of the form::

        {"channel": "market", "event_type": "<evt>", "raw": <original dict>, ...}

    The ``raw`` field is the unmodified event payload so callers can pick out
    any field without us having to enumerate them all here.
    """

    def __init__(
        self,
        asset_ids: list[str],
        *,
        custom_feature_enabled: bool = True,
    ) -> None:
        self.asset_ids = list(asset_ids)
        self.custom_feature_enabled = custom_feature_enabled
        self._stop = asyncio.Event()
        self._counter: Counter[str] = Counter()

    # ------------------------------------------------------------------ #
    # public                                                              #
    # ------------------------------------------------------------------ #

    @property
    def counters(self) -> dict[str, int]:
        """Cumulative count of dispatched messages by ``event_type``.

        Exposed so the ingest service can emit a Prometheus-style counter
        log periodically without us needing a metrics dependency in here.
        """
        return dict(self._counter)

    def stop(self) -> None:
        self._stop.set()

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed events forever; reconnect with exponential backoff."""
        backoff = 1.0
        url = f"{_ws_base()}/market"
        sub_payload = {
            "type": "market",
            "assets_ids": self.asset_ids,
            "custom_feature_enabled": self.custom_feature_enabled,
        }

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=15,
                    ping_timeout=15,
                    close_timeout=5,
                    max_size=2**22,  # 4MB — book snapshots for popular markets are big
                ) as ws:
                    await ws.send(json.dumps(sub_payload))
                    log.info(
                        "clob_ws_subscribed",
                        channel="market",
                        n=len(self.asset_ids),
                        custom_feature=self.custom_feature_enabled,
                        url=url,
                    )
                    backoff = 1.0
                    async for raw in ws:
                        # Reply to application-level PING text frames.  The
                        # `websockets` lib already handles control-frame pings
                        # but Polymarket sends a literal text "PING" payload.
                        if isinstance(raw, (bytes, bytearray)):
                            raw = raw.decode("utf-8", errors="ignore")
                        stripped = raw.strip() if isinstance(raw, str) else ""
                        if stripped.upper() == "PING":
                            try:
                                await ws.send("PONG")
                            except Exception:  # noqa: BLE001
                                pass
                            continue

                        for evt in self._iter_events(raw, channel="market"):
                            yield evt
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "clob_ws_disconnected",
                    channel="market",
                    err=str(exc),
                    err_type=type(exc).__name__,
                    backoff=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ------------------------------------------------------------------ #
    # internals                                                           #
    # ------------------------------------------------------------------ #

    def _iter_events(self, raw: str, *, channel: str) -> list[dict[str, Any]]:
        """Parse one frame and return zero-or-more normalized events.

        Polymarket batches events into a JSON list per frame.  Some
        deployments send a bare object instead, so we handle both shapes.
        Malformed frames are logged once and dropped.
        """
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("clob_ws_bad_json", channel=channel, err=str(exc))
            return []

        if isinstance(parsed, dict):
            items = [parsed]
        elif isinstance(parsed, list):
            items = parsed
        else:
            log.warning("clob_ws_unexpected_payload", channel=channel, py_type=type(parsed).__name__)
            return []

        out: list[dict[str, Any]] = []
        known = MARKET_EVENTS if channel == "market" else USER_EVENTS
        for item in items:
            if not isinstance(item, dict):
                continue
            evt = item.get("event_type") or item.get("type") or "unknown"
            self._counter[evt] += 1
            if evt not in known and self._counter[evt] == 1:
                log.info("clob_ws_unknown_event", channel=channel, event_type=evt)
            out.append({"channel": channel, "event_type": evt, "raw": item})
        return out


class ClobUserWebSocket:
    """Subscribe to the L2-authenticated ``/ws/user`` channel.

    Hook left in place for the future: this is the only channel that emits
    wallet-attributed fills, but it requires API credentials minted via
    ``POST /auth/api-key`` for the wallet whose trades you want to see.  We
    do not currently own arbitrary tracked wallets, so this class is not
    spun up by the ingest service yet; it exists so callers (a future signer
    integration) can drop in without redesigning the consumer.

    ``markets`` is a list of **condition ids** (0x... hex), NOT token ids
    — this differs from the market channel.
    """

    def __init__(
        self,
        markets: list[str],
        *,
        api_key: str,
        secret: str,
        passphrase: str,
    ) -> None:
        self.markets = list(markets)
        self._auth = {"apiKey": api_key, "secret": secret, "passphrase": passphrase}
        self._stop = asyncio.Event()
        self._counter: Counter[str] = Counter()

    @property
    def counters(self) -> dict[str, int]:
        return dict(self._counter)

    def stop(self) -> None:
        self._stop.set()

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        backoff = 1.0
        url = f"{_ws_base()}/user"
        sub_payload = {
            "type": "user",
            "markets": self.markets,
            "auth": self._auth,
        }

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=15,
                    ping_timeout=15,
                    close_timeout=5,
                    max_size=2**22,
                ) as ws:
                    await ws.send(json.dumps(sub_payload))
                    log.info(
                        "clob_ws_subscribed",
                        channel="user",
                        n=len(self.markets),
                        url=url,
                    )
                    backoff = 1.0
                    async for raw in ws:
                        if isinstance(raw, (bytes, bytearray)):
                            raw = raw.decode("utf-8", errors="ignore")
                        stripped = raw.strip() if isinstance(raw, str) else ""
                        if stripped.upper() == "PING":
                            try:
                                await ws.send("PONG")
                            except Exception:  # noqa: BLE001
                                pass
                            continue

                        # Reuse the market-channel parsing helper by
                        # constructing a lightweight throwaway instance —
                        # simpler than duplicating the parser body.
                        for evt in _parse_user_frame(raw, self._counter):
                            yield evt
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "clob_ws_disconnected",
                    channel="user",
                    err=str(exc),
                    err_type=type(exc).__name__,
                    backoff=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


def _parse_user_frame(raw: str, counter: Counter[str]) -> list[dict[str, Any]]:
    """Parse a /ws/user frame into normalized events.

    Mirrors ``ClobWebSocket._iter_events`` but with the user-channel event
    vocabulary.  Lives at module scope so the user-channel class can stay
    stateless beyond its counter.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("clob_ws_bad_json", channel="user", err=str(exc))
        return []

    if isinstance(parsed, dict):
        items = [parsed]
    elif isinstance(parsed, list):
        items = parsed
    else:
        log.warning("clob_ws_unexpected_payload", channel="user", py_type=type(parsed).__name__)
        return []

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        evt = item.get("event_type") or "unknown"
        counter[evt] += 1
        if evt not in USER_EVENTS and counter[evt] == 1:
            log.info("clob_ws_unknown_event", channel="user", event_type=evt)
        out.append({"channel": "user", "event_type": evt, "raw": item})
    return out
