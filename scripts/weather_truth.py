"""weather_truth.py — STEP 2b-1: reconstruct the ACTUAL resolved highs, independent
of the corrupt fills.

Enumerate recent weather markets from the gamma-sourced market CATALOG (the `markets`
table — gamma metadata, NOT the corrupt `fills`), resolve them CONCURRENTLY and live via
gamma, reassemble each city-day "ladder" of buckets, and find the one bucket that
resolved YES — that bucket IS where the day's high landed (to ~1°C / 1–2°F). The output
is the clean ground-truth dataset the forecast-grading step (2b-2) measures against.
No forecasts, no fills.

The catalog holds ~thousands of weather markets (months of history), so default to the
last 14 days; widen with --days. Run on the VPS:
    docker compose exec -T executor python -m scripts.weather_truth --days 14
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from collections import defaultdict

from scripts.weather_pnl import parse_q
from scripts.weather_recon import is_weather


def parse_bucket(s):
    """A temperature bucket label → {lo, hi, mid, unit, open}. Examples:
    '28°C'→(27.5,28.5) point; 'between 92-93°F'→(92,93); '37°C or below'→(−,37);
    '40°C or above'→(40,+). Returns None if no number is present."""
    t = (s or "").strip()
    nums = [int(x) for x in re.findall(r"\d+", t)]
    if not nums:
        return None
    unit = "F" if "F" in t.upper() else "C"
    low = "below" in t.lower()
    high = "above" in t.lower()
    if len(nums) >= 2 and "-" in t:
        lo, hi, mid, is_open = float(nums[0]), float(nums[1]), (nums[0] + nums[1]) / 2.0, False
    elif low:
        lo, hi, mid, is_open = float(nums[0]) - 60, float(nums[0]), float(nums[0]), True
    elif high:
        lo, hi, mid, is_open = float(nums[0]), float(nums[0]) + 60, float(nums[0]), True
    else:
        lo, hi, mid, is_open = nums[0] - 0.5, nums[0] + 0.5, float(nums[0]), False
    return {"lo": lo, "hi": hi, "mid": mid, "unit": unit, "open": is_open}


def actual_high(legs):
    """legs: list of {bucket, parsed, yes}. Returns (status, yes_legs):
    'clean' with exactly one YES (the high landed there), 'none'/'multi' otherwise."""
    yes = [x for x in legs if x["yes"]]
    if len(yes) == 1:
        return ("clean", yes)
    return (("none" if not yes else "multi"), yes)


def _yes_won(m):
    """True/False/None from a gamma market dict: did the YES (outcome[0]) leg pay out?"""
    if not m or not m.get("closed"):
        return None
    try:
        p = m.get("outcomePrices")
        p = json.loads(p) if isinstance(p, str) else p
        return float(p[0]) > 0.5
    except (TypeError, ValueError, IndexError):
        return None


async def reconstruct(*, days, cap, conc):
    """DB-free ground truth: resolve recent weather markets via gamma and reassemble
    ladders. Returns (clean, none_, multi) lists of ladder records; each record is
    {key:(city,date,kind), legs, yes, n_buckets}. Reused by the forecast grader."""
    from datetime import datetime, timedelta, timezone

    from polybot.clients import GammaClient
    from polybot.db import session_scope
    from polybot.models import Market
    from sqlalchemy import select

    now = datetime.now(tz=timezone.utc)
    async with session_scope() as s:
        rows = (await s.execute(
            select(Market.market_id, Market.question).where(
                Market.question.op("~*")(r"temperature"),
                Market.end_date.is_not(None),
                Market.end_date >= now - timedelta(days=days),
                Market.end_date <= now,
            )
        )).all()
    cat = [(r.market_id, r.question) for r in rows if is_weather(r.question)]
    cat.sort(key=lambda x: x[0])
    if cap:
        cat = cat[:cap]
    print(f"catalog weather markets (last {days}d): {len(cat)} — resolving live via gamma "
          f"@ {conc} concurrent…")

    g = GammaClient()
    sem = asyncio.Semaphore(conc)

    async def resolve(mid):
        async with sem:
            try:
                out = await g.get("/markets", params={"condition_ids": mid, "closed": "true"})
            except Exception:  # noqa: BLE001
                return mid, None
        m = out[0] if isinstance(out, list) and out else None
        return mid, _yes_won(m)

    try:
        res = dict(await asyncio.gather(*[resolve(mid) for mid, _ in cat]))
    finally:
        await g.close()

    legs, unresolved = [], 0
    for mid, q in cat:
        yw = res.get(mid)
        kind, city, bucket, date = parse_q(q)
        pb = parse_bucket(bucket) if bucket else None
        if yw is None:
            unresolved += 1
        if not (city and date and pb is not None and yw is not None):
            continue
        legs.append({"city": city, "date": date, "kind": kind, "bucket": bucket,
                     "parsed": pb, "yes": yw})

    groups = defaultdict(list)
    for x in legs:
        groups[(x["city"], x["date"], x["kind"])].append(x)

    clean, none_, multi = [], [], []
    for key, gl in groups.items():
        status, yes = actual_high(gl)
        rec = {"key": key, "legs": gl, "yes": yes, "n_buckets": len(gl)}
        (clean if status == "clean" else none_ if status == "none" else multi).append(rec)

    print(f"resolved buckets: {len(legs)}  (still-open/unresolved: {unresolved})  "
          f"→ {len(groups)} city-day ladders")
    print(f"GROUND TRUTH usable (exactly one YES bucket): {len(clean)}   "
          f"unclear: none-yes={len(none_)} multi-yes={len(multi)} "
          f"(partial ladders / boundary buckets)")
    return clean, none_, multi


async def run(*, days, cap, conc):
    clean, none_, multi = await reconstruct(days=days, cap=cap, conc=conc)

    print("\nACTUAL HIGHS (city-day → winning bucket = where the high landed):")
    for rec in sorted(clean, key=lambda r: (r["key"][1], r["key"][0])):
        city, date, kind = rec["key"]
        y = rec["yes"][0]
        print(f"  {date:>7} {city:<16} {kind:<7} high = {y['bucket']:<18} "
              f"(ladder of {rec['n_buckets']} buckets we have)")

    if none_:
        print(f"\nnote: {len(none_)} city-days had NO yes-bucket in our catalog — the high "
              "landed in a bucket we didn't ingest (need the full event ladder for those).")
    if multi:
        print(f"⚠ {len(multi)} ladders with >1 YES (boundary/overlap — inspect):")
        for rec in multi[:5]:
            city, date, kind = rec["key"]
            print(f"  {date} {city} {kind}: YES = {', '.join(y['bucket'] for y in rec['yes'])}")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Step 2b-1: reconstruct actual resolved highs (catalog + gamma)")
    ap.add_argument("--days", type=int, default=14, help="lookback window on market end_date")
    ap.add_argument("--cap", type=int, default=0, help="limit markets resolved (0 = all in window)")
    ap.add_argument("--conc", type=int, default=20, help="concurrent gamma resolutions")
    args = ap.parse_args()
    asyncio.run(run(days=args.days, cap=args.cap, conc=args.conc))


if __name__ == "__main__":
    main()
