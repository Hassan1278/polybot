"""Ingest service entrypoint.

Schedules six loops concurrently:
  - market_ingest          : every 5 min, sync active markets + liquidity
  - leaderboard_scraper    : every 30 min, refresh top wallets per category
  - trade_ingest           : every 15 min, backfill recent trades for tracked wallets
  - attribution_heartbeat  : every 5 min, silent-failure detector — alerts if no
                             trades attribute to tracked wallets in the lookback
                             window (proxyWallet schema break canary)
  - resolution_check       : every 10 min, flips markets.resolved=true for
                             open paper positions past their end_date so the
                             executor's settle loop can redeem them
  - live_listener          : continuous CLOB WS subscription
"""

from __future__ import annotations

import asyncio
import signal

from polybot.health_server import HealthBeacon, run_health_server
from polybot.logging import get_logger
from services.ingest.jobs.attribution_heartbeat import run_attribution_heartbeat
from services.ingest.jobs.leaderboard_scraper import run_leaderboard
from services.ingest.jobs.live_listener import run_live_listener
from services.ingest.jobs.market_ingest import run_market_ingest
from services.ingest.jobs.resolution_check import run_resolution_check
from services.ingest.jobs.trade_ingest import run_trade_ingest

log = get_logger(__name__)

# Healthy if ANY of the 6 jobs has produced a heartbeat in the last 10 min.
# market_ingest and attribution_heartbeat run every 5 min so this floor is
# tight enough to catch a real stall but loose enough to ignore one missed
# beat. live_listener heartbeats continuously while WS is connected.
_BEACON = HealthBeacon(name="ingest", stale_after_seconds=600)


async def _every(name: str, seconds: int, coro_factory):
    while True:
        try:
            await coro_factory()
            _BEACON.heartbeat(last_job=name)
        except Exception:
            log.exception("ingest_job_failed", job=name)
        await asyncio.sleep(seconds)


async def main() -> None:
    log.info("ingest_starting")
    stop = asyncio.Event()

    def _shutdown(*_):
        log.info("ingest_stopping")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass  # Windows

    tasks = [
        asyncio.create_task(_every("market_ingest",         300,  run_market_ingest)),
        asyncio.create_task(_every("leaderboard",           1800, run_leaderboard)),
        asyncio.create_task(_every("trade_ingest",          900,  run_trade_ingest)),
        asyncio.create_task(_every("attribution_heartbeat", 300,  run_attribution_heartbeat)),
        asyncio.create_task(_every("resolution_check",      600,  run_resolution_check)),
        asyncio.create_task(run_live_listener()),
        asyncio.create_task(run_health_server(_BEACON, port=8081)),
    ]

    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
