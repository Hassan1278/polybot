"""Flip the kill-switch from the command line. Use as a last resort."""

from __future__ import annotations

import asyncio

import click

from polybot.clients import ClobClient
from polybot.config import settings
from polybot.logging import get_logger
from polybot.redis_bus import kill_clear, kill_set, kill_status

log = get_logger(__name__)


@click.group()
def cli() -> None:
    """polybot kill"""


@cli.command("set")
@click.option("--reason", default="cli")
def set_cmd(reason: str) -> None:
    async def _go() -> None:
        await kill_set(reason)
        if settings.is_live and settings.can_sign:
            try:
                c = ClobClient()
                try:
                    await c.cancel_all()
                finally:
                    await c.close()
            except Exception:
                log.exception("cancel_all_failed")
        log.warning("kill_switch_set", reason=reason)
    asyncio.run(_go())


@cli.command("clear")
@click.option("--by", default="cli")
def clear_cmd(by: str) -> None:
    asyncio.run(kill_clear(by))
    log.info("kill_switch_cleared", by=by)


@cli.command("status")
def status_cmd() -> None:
    s = asyncio.run(kill_status())
    print(s or "not_active")


if __name__ == "__main__":
    cli()
