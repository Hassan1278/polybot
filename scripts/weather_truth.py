"""weather_truth.py — STEP 2b-1: reconstruct the ACTUAL resolved highs, independent
of our (corrupt) DB.

Pulls resolved temperature markets straight from gamma, reassembles each city-day
"ladder" of buckets, and finds the one bucket that resolved YES — that bucket IS where
the day's high landed (to ~1°C / 1–2°F). The output is the clean ground-truth dataset
the forecast-grading step (2b-2) will measure against. No forecasts, no DB fills.

Run on the VPS:
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


async def run(*, days, cap_pages):
    from datetime import datetime, timedelta, timezone

    from polybot.clients import GammaClient
    now = datetime.now(tz=timezone.utc)
    g = GammaClient()
    raw = []
    try:
        for page in range(cap_pages):
            mk = await g.get("/markets", params={
                "closed": "true", "limit": 500, "offset": page * 500,
                "end_date_min": (now - timedelta(days=days)).isoformat(),
                "end_date_max": now.isoformat(),
                "order": "endDate", "ascending": "false",
            }) or []
            if not mk:
                break
            raw.extend(mk)
            if len(mk) < 500:
                break
    finally:
        await g.close()

    temp = [m for m in raw if is_weather(m.get("question", ""))]
    legs = []
    for m in temp:
        kind, city, bucket, date = parse_q(m.get("question", ""))
        pb = parse_bucket(bucket) if bucket else None
        yw = _yes_won(m)
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

    print(f"\npulled {len(raw)} closed markets (last {days}d) → {len(temp)} temperature, "
          f"{len(legs)} resolved buckets → {len(groups)} city-day ladders")
    print(f"GROUND TRUTH usable (exactly one YES bucket): {len(clean)}   "
          f"unclear: none-yes={len(none_)} multi-yes={len(multi)} "
          f"(incomplete ladders / boundary buckets)")

    print("\nACTUAL HIGHS (city-day → winning bucket = where the high landed):")
    for rec in sorted(clean, key=lambda r: (r["key"][1], r["key"][0])):
        city, date, kind = rec["key"]
        y = rec["yes"][0]
        print(f"  {date:>7} {city:<16} {kind:<7} high = {y['bucket']:<18} "
              f"(ladder of {rec['n_buckets']} buckets)")

    if multi:
        print(f"\n⚠ {len(multi)} ladders with >1 YES (boundary/overlap — inspect a few):")
        for rec in multi[:5]:
            city, date, kind = rec["key"]
            print(f"  {date} {city} {kind}: YES = {', '.join(y['bucket'] for y in rec['yes'])}")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Step 2b-1: reconstruct actual resolved highs from gamma")
    ap.add_argument("--days", type=int, default=14, help="lookback window of resolved markets")
    ap.add_argument("--cap-pages", type=int, default=20, help="max gamma pages (500/page)")
    args = ap.parse_args()
    asyncio.run(run(days=args.days, cap_pages=args.cap_pages))


if __name__ == "__main__":
    main()
