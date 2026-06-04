"""Pull N days of trade history for every tracked wallet."""

from __future__ import annotations

import asyncio

import click

from polybot.logging import get_logger
from services.ingest.jobs.trade_ingest import run_trade_ingest

log = get_logger(__name__)


@click.command()
@click.option("--days", default=30, type=int, help="(informational only — Data API returns last N trades, not N days)")
def main(days: int) -> None:
    log.info("backfill_starting", days=days)
    asyncio.run(run_trade_ingest(concurrency=5))


if __name__ == "__main__":
    main()
