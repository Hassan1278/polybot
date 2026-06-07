"""Tiny in-process health server for subscriber services (ingest, signals, executor).

The API service has its own /health and /health/deep endpoints via FastAPI;
the worker services don't run FastAPI, so they expose a minimal aiohttp
server on a fixed internal port (8081). Docker healthchecks curl this
endpoint instead of just checking `pgrep python`.

Usage:
    from polybot.health_server import HealthBeacon, run_health_server
    beacon = HealthBeacon(stale_after_seconds=600)

    # inside the main loop body, on every iteration:
    beacon.heartbeat()

    # in main():
    asyncio.create_task(run_health_server(beacon, port=8081))

The /health response is 200 + JSON when the last heartbeat is within
`stale_after_seconds`, otherwise 503. Docker flips the container to
`unhealthy` after the configured number of consecutive failures.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from aiohttp import web

from polybot.logging import get_logger

log = get_logger(__name__)


@dataclass
class HealthBeacon:
    """Caller updates `last_heartbeat_ts` from their main loop. The health
    server reads it to decide if the service is healthy.

    `stale_after_seconds` should be ~2× the slowest expected loop interval.
    Ingest's slowest tick is leaderboard (30 min) but the beacon should be
    pinged from any job, so 10 min is comfortable. Bump if you see
    spurious "unhealthy" flags.
    """
    name: str
    stale_after_seconds: int = 600
    last_heartbeat_ts: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)

    def heartbeat(self, **kw) -> None:
        self.last_heartbeat_ts = time.time()
        if kw:
            self.extra.update(kw)

    @property
    def lag_seconds(self) -> int:
        return int(time.time() - self.last_heartbeat_ts)

    @property
    def is_healthy(self) -> bool:
        return self.lag_seconds <= self.stale_after_seconds

    def to_dict(self) -> dict:
        return {
            "service": self.name,
            "ok": self.is_healthy,
            "lag_seconds": self.lag_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            **self.extra,
        }


async def run_health_server(beacon: HealthBeacon, *, port: int = 8081) -> None:
    """Run an aiohttp server exposing /health on `port`. Blocks forever."""
    async def _handler(_request: web.Request) -> web.Response:
        payload = beacon.to_dict()
        status = 200 if payload["ok"] else 503
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_get("/health", _handler)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("health_server_started", service=beacon.name, port=port)
    # Sleep forever — runner stays attached to the event loop
    import asyncio
    while True:
        await asyncio.sleep(3600)
