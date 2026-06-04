"""Ingest service entrypoint.

Schedules five loops concurrently:
  - market_ingest          : every 5 min, sync active markets + liquidity
  - leaderboard_scraper    : every 30 min, refresh top wallets per category
  - trade_ingest           : every 15 min, backfill recent trades for tracked wallets
  - attribution_heartbeat  : every 5 min, silent-failure detector — alerts if no
                             trades attribute to tracked wallets in the lookback
                             window (proxyWallet schema break canary)
  - live_listener          : continuous CLOB WS subscription
"""

from __future__ import annotations

import asyncio
import signal

from polybot.logging import get_logger
from services.ingest.jobs.attribution_heartbeat import run_attribution_heartbeat
from services.ingest.jobs.leaderboard_scraper import run_leaderboard
from services.ingest.jobs.live_listener import run_live_listener
from services.ingest.jobs.market_ingest import run_market_ingest
from services.ingest.jobs.trade_ingest import run_trade_ingest

log = get_logger(__name__)


async def _every(name: str, seconds: int, coro_factory):
    while True:
        try:
            await coro_factory()
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
        asyncio.create_task(run_live_listener()),
    ]

    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
