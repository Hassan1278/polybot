"""One-shot diagnostic for the stats recompute."""

from __future__ import annotations

import asyncio
import traceback

from sqlalchemy import select

from polybot.db import session_scope
from polybot.models import Wallet
from services.signals.stats_loop import _compute_for_addr


async def main() -> None:
    async with session_scope() as s:
        addrs = [r[0] for r in (await s.execute(
            select(Wallet.address).where(Wallet.is_active.is_(True))
        )).all()]
    print(f"recomputing for {len(addrs)} wallets")
    fail = 0
    first_err = None
    for a in addrs:
        try:
            await _compute_for_addr(a)
        except Exception:
            fail += 1
            if first_err is None:
                first_err = (a, traceback.format_exc())
    print(f"done; {fail}/{len(addrs)} failures")
    if first_err:
        print(f"\nFIRST FAILURE: {first_err[0]}")
        print(first_err[1])


if __name__ == "__main__":
    asyncio.run(main())
