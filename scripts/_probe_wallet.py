"""Decompose a wallet's positions to explain large total-PnL gaps."""

from __future__ import annotations

import asyncio
import sys

from polybot.clients import DataClient


async def main(addr: str) -> None:
    d = DataClient()
    try:
        positions = await d.positions(addr, limit=500)
    finally:
        await d.close()

    n = len(positions)
    open_loss = [p for p in positions if abs(float(p.get("size", 0))) > 0.01 and float(p.get("cashPnl", 0)) < 0]
    open_gain = [p for p in positions if abs(float(p.get("size", 0))) > 0.01 and float(p.get("cashPnl", 0)) > 0]
    flat      = [p for p in positions if abs(float(p.get("size", 0))) <= 0.01]
    neg_risk  = [p for p in positions if p.get("negativeRisk")]
    redeem    = [p for p in positions if p.get("redeemable")]

    def s(lst, k):
        return sum(float(p.get(k, 0) or 0) for p in lst)

    print(f"wallet: {addr}")
    print(f"total positions:     {n}")
    print(f"  flat (size~0):     {len(flat):3d}    closed, no more exposure")
    print(f"  open + losing:     {len(open_loss):3d}")
    print(f"  open + winning:    {len(open_gain):3d}")
    print(f"  negativeRisk:      {len(neg_risk):3d}    multi-outcome / hedge markets")
    print(f"  redeemable:        {len(redeem):3d}    settled but not yet redeemed")
    print()
    print(f"cashPnl total:       ${s(positions,'cashPnl'):>15,.0f}")
    print(f"  on open losers:    ${s(open_loss,'cashPnl'):>15,.0f}")
    print(f"  on open winners:   ${s(open_gain,'cashPnl'):>15,.0f}")
    print(f"  on flat:           ${s(flat,'cashPnl'):>15,.0f}")
    print(f"  on negativeRisk:   ${s(neg_risk,'cashPnl'):>15,.0f}")
    print()
    print(f"realizedPnl total:   ${s(positions,'realizedPnl'):>15,.0f}")
    print(f"initialValue total:  ${s(positions,'initialValue'):>15,.0f}")
    print(f"currentValue total:  ${s(positions,'currentValue'):>15,.0f}")
    print()
    losers = sorted(positions, key=lambda p: float(p.get("cashPnl", 0)))[:3]
    for p in losers:
        print(f"  worst: cashPnl=${float(p['cashPnl']):>10,.0f}  "
              f"initVal=${float(p['initialValue']):>9,.0f}  "
              f"curVal=${float(p['currentValue']):>9,.0f}  "
              f"negRisk={p.get('negativeRisk')}  "
              f"size={float(p['size']):>9,.0f}")
        print(f"         {p.get('title','')[:90]}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
