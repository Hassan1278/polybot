"""One-shot CLI wrapper around the leaderboard scraper."""

from __future__ import annotations

import asyncio

import click

from polybot.logging import get_logger
from services.ingest.jobs.leaderboard_scraper import run_leaderboard

log = get_logger(__name__)


@click.command()
@click.option("--top", default=30, type=int, help="Top N per category (overrides categories.yaml)")
def main(top: int) -> None:
    log.info("discover_wallets_starting", top=top)
    asyncio.run(run_leaderboard())


if __name__ == "__main__":
    main()
