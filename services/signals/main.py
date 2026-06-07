from __future__ import annotations

import asyncio

from polybot.health_server import HealthBeacon, run_health_server
from polybot.logging import get_logger
from services.signals.correlation_loop import correlation_loop
from services.signals.stats_loop import stats_loop

log = get_logger(__name__)

# Signals service is event-driven (waits on Redis trade:new). When the bus
# is quiet between trade_ingest bursts (15-min cycles), the loops sit idle
# — that's normal. 20-min staleness window allows 1 full quiet cycle
# plus startup buffer before flagging unhealthy.
_BEACON = HealthBeacon(name="signals", stale_after_seconds=1200)


async def _wrap_loop(name: str, coro):
    """Heartbeat-decorated wrapper. Each iteration of the underlying loop
    is supposed to call `_BEACON.heartbeat()` itself (see correlation_loop
    / stats_loop). This wrapper is just the supervisor that survives one
    iteration crashing — we don't restart automatically because that would
    mask bugs; instead we let the loop log the exception and the health
    beacon will go stale, triggering Docker to restart the container."""
    try:
        await coro
    except Exception:
        log.exception("signals_loop_crashed", loop=name)
        raise


async def main() -> None:
    log.info("signals_starting")
    # Heartbeat NOW so the beacon is fresh while the loops warm up.
    _BEACON.heartbeat(state="warming_up")
    await asyncio.gather(
        _wrap_loop("correlation", correlation_loop(_BEACON)),
        _wrap_loop("stats", stats_loop(_BEACON)),
        run_health_server(_BEACON, port=8081),
    )


if __name__ == "__main__":
    asyncio.run(main())
