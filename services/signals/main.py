from __future__ import annotations

import asyncio

from polybot.logging import get_logger
from services.signals.correlation_loop import correlation_loop
from services.signals.stats_loop import stats_loop

log = get_logger(__name__)


async def main() -> None:
    log.info("signals_starting")
    await asyncio.gather(
        correlation_loop(),
        stats_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
