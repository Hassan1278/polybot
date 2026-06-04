"""Tiny pub/sub wrapper. Channels:

  trade:new       — payload {wallet, market_id, side, size, price, ts}
  signal:new      — payload {id, market_id, side, wallets[], score, ...}
  fill:new        — payload {id, signal_id, mode, side, size, price, status}
  kill:set        — payload {reason}
  kill:clear      — payload {by}
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as redis

from polybot.config import settings

KILL_KEY = "polybot:kill_switch"

_pool = redis.ConnectionPool.from_url(settings.redis_url, decode_responses=True)


def client() -> redis.Redis:
    return redis.Redis(connection_pool=_pool)


async def publish(channel: str, payload: dict[str, Any]) -> None:
    r = client()
    await r.publish(channel, json.dumps(payload, default=str))


async def subscribe(channel: str) -> AsyncIterator[dict[str, Any]]:
    r = client()
    pubsub = r.pubsub()
    await pubsub.subscribe(channel)
    async for msg in pubsub.listen():
        if msg["type"] != "message":
            continue
        try:
            yield json.loads(msg["data"])
        except json.JSONDecodeError:
            continue


# ---- kill switch ------------------------------------------------------------

async def kill_set(reason: str) -> None:
    r = client()
    await r.set(KILL_KEY, reason)
    await publish("kill:set", {"reason": reason})


async def kill_clear(by: str) -> None:
    r = client()
    await r.delete(KILL_KEY)
    await publish("kill:clear", {"by": by})


async def kill_status() -> str | None:
    r = client()
    return await r.get(KILL_KEY)
