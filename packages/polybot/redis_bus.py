"""Redis message bus — pub/sub for high-volume analytics, Streams for the
critical signal-execution path.

Channels & semantics
====================

Pub/Sub (fire-and-forget — subscriber-down = message lost. Acceptable for
high-throughput analytics where a missed event is OK and the publisher
can't afford to block on slow consumers):

  trade:new       — wallet trades from ingest          (analytics)
  fill:new        — completed fills from executor      (dashboard SSE)
  candidate:new   — pre-gate clusters from signals     (debug observability)
  kill:set        — kill-switch flip                   (control plane)
  kill:clear      — kill-switch reset                  (control plane)

Streams (guaranteed delivery — XACK after successful processing, XAUTOCLAIM
re-routes crashed consumers, XADD-to-DLQ for poison messages. Used for
signal-execution path where every message is real money at stake):

  signal:new      — gate-passed signals → executor
  signal:new:dlq  — messages that exhausted retry budget, for human triage

Stream operations (use these for critical paths):
  - xpublish(stream, payload) → returns the assigned message id
  - xconsume(stream, group, consumer) → async generator of (msg_id, payload)
  - xack(stream, group, msg_id) → mark processed
  - xdlq(stream, payload, error) → move poison message to DLQ + ack original
  - xautoclaim(stream, group, consumer, min_idle_ms) → re-assign abandoned
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as redis
import redis.exceptions as redis_exc

from polybot.config import settings
from polybot.logging import get_logger

log = get_logger(__name__)

KILL_KEY = "polybot:kill_switch"

_pool = redis.ConnectionPool.from_url(settings.redis_url, decode_responses=True)


def client() -> redis.Redis:
    return redis.Redis(connection_pool=_pool)


# ---- Pub/Sub (legacy, fire-and-forget) --------------------------------------


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


# ---- Streams (durable, ack/dlq) ---------------------------------------------

# Cap each stream at ~10k entries via MAXLEN with `approximate=True` so Redis
# can trim in O(1). At our throughput (~100-200 fills/day) that's weeks of
# history before truncation — plenty for replay debugging.
_STREAM_MAXLEN = 10_000


async def xpublish(stream: str, payload: dict[str, Any]) -> str:
    """Append `payload` to `stream`. Returns the auto-generated msg id.

    Use this instead of `publish()` for any message whose loss would mean
    real money lost or untracked side effects."""
    r = client()
    msg_id = await r.xadd(
        stream,
        {"data": json.dumps(payload, default=str)},
        maxlen=_STREAM_MAXLEN,
        approximate=True,
    )
    return msg_id


async def _ensure_group(r: redis.Redis, stream: str, group: str) -> None:
    """Create consumer group if missing (idempotent). `mkstream=True` so the
    stream itself is created on first call — avoids "stream not found"."""
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
        log.info("redis_stream_group_created", stream=stream, group=group)
    except redis_exc.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def xconsume(
    stream: str,
    group: str,
    consumer: str,
    *,
    count: int = 16,
    block_ms: int = 5000,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield (msg_id, payload) for new messages on `stream` for this consumer.

    Caller MUST call `xack(stream, group, msg_id)` after successful
    processing, OR `xdlq(stream, payload, error)` to route poison messages
    out of the main stream. A message that is neither ack'd nor DLQ'd
    stays in the consumer's Pending Entries List and will be re-delivered
    after `xautoclaim`.
    """
    r = client()
    await _ensure_group(r, stream, group)
    while True:
        try:
            res = await r.xreadgroup(
                group, consumer, {stream: ">"},
                count=count, block=block_ms,
            )
        except redis_exc.ResponseError as e:
            if "NOGROUP" in str(e):
                # Stream was wiped under us — recreate group and retry
                await _ensure_group(r, stream, group)
                continue
            raise
        if not res:
            continue
        for _stream_name, entries in res:
            for msg_id, fields in entries:
                raw = fields.get("data") if isinstance(fields, dict) else None
                if raw is None:
                    log.warning("xconsume_empty_payload", msg_id=msg_id)
                    await r.xack(stream, group, msg_id)
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    log.error("xconsume_bad_json", msg_id=msg_id, raw=raw[:200])
                    # Bad JSON is a poison message — route to DLQ
                    await xdlq(stream, {"raw": raw}, "json_decode_error", _msg_id=msg_id)
                    continue
                yield msg_id, payload


async def xack(stream: str, group: str, msg_id: str) -> None:
    """Mark a message as successfully processed."""
    await client().xack(stream, group, msg_id)


async def xdlq(
    stream: str,
    payload: dict[str, Any],
    error: str,
    *,
    _msg_id: str | None = None,
) -> str:
    """Move a poisonous message to `{stream}:dlq` and ack the original.

    The DLQ entry captures: original payload, error description, timestamp.
    Operators triage by `XREVRANGE signal:new:dlq + -`. After fixing the
    root cause, replay via `xpublish('signal:new', payload)`.
    """
    r = client()
    dlq_id = await r.xadd(
        f"{stream}:dlq",
        {
            "data": json.dumps(payload, default=str),
            "err": error[:512],
            "orig_id": _msg_id or "",
        },
        maxlen=_STREAM_MAXLEN,
        approximate=True,
    )
    if _msg_id is not None:
        # Best-effort ack on the original — if it doesn't exist (already
        # ack'd from a prior attempt) Redis returns 0 silently.
        for group in ("executors",):
            try:
                await r.xack(stream, group, _msg_id)
            except Exception:  # noqa: BLE001
                pass
    return dlq_id


async def xautoclaim(
    stream: str,
    group: str,
    consumer: str,
    *,
    min_idle_ms: int = 60_000,
    count: int = 32,
) -> int:
    """Reclaim messages that have been pending for longer than `min_idle_ms`.

    If consumer A pulls a message then crashes, the message stays in A's
    PEL and is never re-delivered. Periodically call this from healthy
    consumer B to reclaim those messages. Returns the number reclaimed.
    """
    r = client()
    try:
        next_id, claimed, deleted = await r.xautoclaim(
            stream, group, consumer,
            min_idle_time=min_idle_ms,
            count=count,
        )
        n = len(claimed) if claimed else 0
        if n:
            log.warning(
                "redis_stream_autoclaim_reassigned",
                stream=stream, count=n, consumer=consumer,
            )
        return n
    except redis_exc.ResponseError as e:
        if "NOGROUP" in str(e):
            return 0
        raise


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
