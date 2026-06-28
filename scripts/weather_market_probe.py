"""weather_market_probe.py — STEP 2c PROBE: can we get historical bucket prices, and is
the market centered on the settled (WU) high or biased low?

For one resolved city-day ladder, pull each bucket's market price ~Nh before resolution
(CLOB /prices-history — independent of the corrupt fills) and lay it next to the bucket
that actually won. First look at whether the market's implied high sits ON the settled
value or BELOW it (sharing the raw-forecast blind spot we measured — which, if the high
buckets are underpriced, is the edge).

Run on the VPS:
    docker compose exec -T executor python -m scripts.weather_market_probe --city London --date "June 21"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from scripts.weather_pnl import parse_q
from scripts.weather_recon import is_weather
from scripts.weather_truth import parse_bucket


def _sample_at(history, target_ts):
    """(price, ts) of the history point closest to target_ts; (None, None) if empty."""
    pts = [(int(h["t"]), float(h["p"])) for h in (history or []) if "t" in h and "p" in h]
    if not pts:
        return (None, None)
    t, p = min(pts, key=lambda tp: abs(tp[0] - target_ts))
    return (p, t)


async def run(*, city, date, hours_before):
    from datetime import datetime

    from sqlalchemy import select

    from polybot.clients import ClobClient, GammaClient
    from polybot.db import session_scope
    from polybot.models import Market

    async with session_scope() as s:
        rows = (await s.execute(
            select(Market.market_id, Market.question)
            .where(Market.question.ilike(f"%temperature in {city}%{date}%"))
        )).all()
    rows = [(m, q) for m, q in rows if is_weather(q)]
    if not rows:
        print(f"no catalog markets for '{city}' '{date}'")
        return
    print(f"{city} {date}: {len(rows)} buckets in catalog")

    g, clob = GammaClient(), ClobClient()
    out = []
    try:
        for mid, q in rows:
            m = await g.market_by_condition_id(mid)
            if not m:
                continue
            toks = m.get("clobTokenIds")
            toks = json.loads(toks) if isinstance(toks, str) else toks
            end = m.get("endDate")
            px = m.get("outcomePrices")
            px = json.loads(px) if isinstance(px, str) else px
            if not toks or not end:
                continue
            end_ts = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp())
            yes_won = (float(px[0]) > 0.5) if (m.get("closed") and px) else None
            _, _, bucket, _ = parse_q(q)
            hist = await clob.price_history(str(toks[0]), interval="max", fidelity=60)
            p, ts = _sample_at(hist, end_ts - hours_before * 3600)
            age_h = (end_ts - ts) / 3600 if ts else None
            out.append((bucket, p, age_h, yes_won, len(hist or [])))
    finally:
        await g.close()
        await clob.close()

    def _mid(b):
        pb = parse_bucket(b)
        return pb["mid"] if pb else 0.0

    print(f"\n{'bucket':>18} {'mkt P':>7} {'sampled@':>9} {'won?':>5} {'pts':>5}")
    for bucket, p, age, won, n in sorted(out, key=lambda x: _mid(x[0])):
        ps = f"{p:.3f}" if p is not None else "   —"
        ah = f"-{age:.0f}h" if age is not None else "  —"
        w = "YES" if won else ("no" if won is not None else "?")
        print(f"{bucket:>18} {ps:>7} {ah:>9} {w:>5} {n:>5}")

    priced = [(b, p) for b, p, _a, _w, _n in out if p is not None]
    actual = next((b for b, _p, _a, w, _n in out if w), None)
    if priced and actual:
        modal = max(priced, key=lambda x: x[1])
        # market-implied expected high (probability-weighted bucket midpoint)
        tot = sum(p for _b, p in priced)
        exp = sum(_mid(b) * p for b, p in priced) / tot if tot else None
        print(f"\nmarket modal bucket @ -{hours_before}h: {modal[0]} (P={modal[1]:.3f})")
        print(f"market-implied E[high] ≈ {exp:.2f}   vs   ACTUAL bucket: {actual} "
              f"(mid {_mid(actual):.1f})")
        if exp is not None:
            gap = _mid(actual) - exp
            print(f"  → actual − market-implied = {gap:+.2f}°   "
                  "(consistently > 0 across many days = market underprices the high = the edge)")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Step 2c probe: historical bucket prices vs settled high")
    ap.add_argument("--city", default="London")
    ap.add_argument("--date", default="June 21")
    ap.add_argument("--hours-before", type=int, default=24)
    args = ap.parse_args()
    asyncio.run(run(city=args.city, date=args.date, hours_before=args.hours_before))


if __name__ == "__main__":
    main()
