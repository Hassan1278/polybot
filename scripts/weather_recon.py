"""weather_recon.py — inventory Polymarket weather markets to scope a forecast-vs-
market edge.

Answers the questions that decide whether the weather thread is viable:
  • Do objectively-resolved weather markets exist, and how many (live vs resolved)?
  • What TYPE (temperature / precip / snow / storm) and in which locations?
  • Are any LIQUID enough to deploy into?
  • CRITICAL: what RESOLUTION SOURCE settles them? Our forecast signal must predict the
    exact same feed/station, so the sample resolution descriptions are the key output.

Observe-only, gamma reads. Run on the VPS (gamma is blocked from the dev sandbox):
    docker compose exec -T executor python -m scripts.weather_recon
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from collections import Counter

# Word-boundary regexes so "ukraine" doesn't match "rain", "snowden" doesn't match
# "snow", etc. Over-precision is fine here — we eyeball the output and widen if needed.
_WEATHER_RE = re.compile(
    r"\b(temperatures?|fahrenheit|celsius|degrees|hottest|warmest|coldest|heat index"
    r"|rain(fall)?|precipitation|snow(fall)?|hurricane|cyclone|tropical storm"
    r"|wind speed|weather)\b|°\s?[fc]\b", re.I)
_TEMP_RE = re.compile(r"\b(temperatures?|fahrenheit|celsius|degrees|hottest|warmest|coldest|heat)\b|°\s?[fc]\b", re.I)
_PRECIP_RE = re.compile(r"\b(rain(fall)?|precipitation)\b", re.I)
_SNOW_RE = re.compile(r"\bsnow(fall)?\b", re.I)
_STORM_RE = re.compile(r"\b(hurricane|cyclone|tropical storm|wind)\b", re.I)

_CITIES = ("nyc", "new york", "los angeles", "chicago", "miami", "london", "paris",
           "houston", "phoenix", "seattle", "boston", "denver", "atlanta", "dallas",
           "san francisco", "washington", "austin", "tokyo", "mumbai", "delhi")


def is_weather(q):
    return bool(_WEATHER_RE.search(q or ""))


def wtype(q):
    t = q or ""
    if _TEMP_RE.search(t):
        return "temperature"
    if _PRECIP_RE.search(t):
        return "precip"
    if _SNOW_RE.search(t):
        return "snow"
    if _STORM_RE.search(t):
        return "storm"
    return "other"


def location(q):
    t = (q or "").lower()
    for c in _CITIES:
        if c in t:
            return c
    return "?"


def _strip_html(s):
    return re.sub(r"<[^>]+>", " ", re.sub(r"\s+", " ", s or "")).strip()


async def run(*, limit):
    from polybot.clients import GammaClient
    g = GammaClient()
    found = []  # (resolved, type, loc, vol, question, end, description)
    try:
        for closed in ("false", "true"):
            offset = 0
            while offset < limit:
                mk = await g.get("/markets", params={
                    "closed": closed, "limit": 100, "offset": offset,
                    "order": "volume", "ascending": "false",
                }) or []
                if not mk:
                    break
                for m in mk:
                    q = m.get("question", "") or ""
                    if not is_weather(q):
                        continue
                    vol = float(m.get("volume") or m.get("volumeNum") or 0)
                    found.append((closed == "true", wtype(q), location(q), vol,
                                  q, m.get("endDate"), m.get("description")))
                offset += 100
    finally:
        await g.close()

    live = [f for f in found if not f[0]]
    resolved = [f for f in found if f[0]]
    print(f"\nWEATHER MARKETS: {len(found)} total  ({len(live)} live, {len(resolved)} resolved)")
    if not found:
        print("none matched — widen keywords or weather isn't a live Polymarket category right now")
        return

    print("\nby type:    ", dict(Counter(f[1] for f in found)))
    print("by location:", dict(Counter(f[2] for f in found).most_common(8)))
    vols = sorted((f[3] for f in found), reverse=True)
    print(f"liquidity:  >$10k: {sum(1 for v in vols if v > 10000)}   "
          f">$1k: {sum(1 for v in vols if v > 1000)}   "
          f"median ${vols[len(vols) // 2]:,.0f}   max ${vols[0]:,.0f}")
    print(f"resolved sample (backtest fuel): {len(resolved)} markets")

    print("\nTOP MARKETS BY VOLUME (question | $vol | ends):")
    for _r, ty, loc, vol, q, end, _d in sorted(found, key=lambda x: -x[3])[:12]:
        print(f"  [{ty:>11}/{loc:<11}] ${vol:>10,.0f}  {(end or '')[:10]}  {q[:70]}")

    print("\nRESOLUTION SOURCES (the feed our forecast must match) — top 4 by volume:")
    for _r, _ty, _loc, vol, q, _end, desc in sorted(found, key=lambda x: -x[3])[:4]:
        print(f"\n  • {q[:90]}  (${vol:,.0f})")
        print(f"    {_strip_html(desc)[:420]}")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Inventory Polymarket weather markets")
    ap.add_argument("--limit", type=int, default=1500, help="markets to scan per closed-state (by volume)")
    args = ap.parse_args()
    asyncio.run(run(limit=args.limit))


if __name__ == "__main__":
    main()
