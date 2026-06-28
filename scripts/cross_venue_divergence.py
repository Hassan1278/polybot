"""cross_venue_divergence.py — RELATIVE price divergence between Polymarket and
Limitless on the same crypto Up/Down market (matched by asset + resolution time).

Absolute gaps hide the structure; we measure in ODDS space (log-odds difference)
and as a ratio — a 0.025-vs-0.006 split is a ~4x relative divergence, while
0.685-vs-0.655 is basically agreement. The decision metric is whether the
divergence is SYSTEMATIC (one venue consistently richer in a region -> a
statistical edge you can fade repeatedly) or symmetric NOISE (thin-market tail
pricing -> nothing to trade).

NOT an arbitrage claim: the venues resolve on DIFFERENT oracles (Limitless =
Chainlink), so identical-looking markets are subtly different products. This is a
statistical-divergence study. Observe-only, no keys, Polymarket reads + Limitless
public REST.

--loop accumulates distinct market-pairs over time (as windows roll) for a real
sample. Usage (on the VPS):
    docker compose exec -T executor python -m scripts.cross_venue_divergence --loop --interval 30
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import re

_LIM_BASE = "https://api.limitless.exchange"


# ── pure core (unit-tested) ──────────────────────────────────────────────────

def logit(p):
    """Log-odds of a probability, clamped off the 0/1 boundary."""
    p = min(max(float(p), 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _duration_seconds(text):
    """Window length (seconds) of an Up/Down market from its title/question, or
    None if undeterminable. Matching REQUIRES the same duration: a 15-min and an
    hourly market ending at the same instant are different bets (different starts)."""
    t = (text or "").lower()
    if "15 min" in t or "15-min" in t:        # check 15 before 5 ("15 min" contains "5 min")
        return 900
    if "5 min" in t or "5-min" in t:
        return 300
    if "hourly" in t or "1 hour" in t or "1-hour" in t:
        return 3600
    if "daily" in t:
        return 86400
    if "weekly" in t:
        return 604800
    m = re.search(r"(\d{1,2}):(\d{2})\s*([ap])m\s*[-–]\s*(\d{1,2}):(\d{2})\s*([ap])m", t)
    if m:
        h1, m1, ap1, h2, m2, ap2 = m.groups()

        def _mins(h, mi, ap):
            return (int(h) % 12 + (12 if ap == "p" else 0)) * 60 + int(mi)

        d = (_mins(h2, m2, ap2) - _mins(h1, m1, ap1)) % (24 * 60)
        return d * 60 if d > 0 else None
    return None


def relative_divergence(lim, poly):
    """Relative-divergence metrics between two probabilities (Limitless vs
    Polymarket), or None if either is out of (0,1). ``log_odds_diff`` > 0 means
    Limitless is richer; ``ratio`` is lim/poly."""
    if lim is None or poly is None or not (0.0 < lim < 1.0) or not (0.0 < poly < 1.0):
        return None
    return {
        "abs_gap": lim - poly,
        "ratio": lim / poly,
        "rel_gap": (lim - poly) / poly,
        "log_odds_diff": logit(lim) - logit(poly),
    }


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def divergence_summary(diffs):
    """``diffs`` = list of log-odds differences (lim − poly) over distinct pairs.
    Returns the systematic-vs-noise verdict: a mean far from 0 relative to its
    standard error = one venue systematically richer (exploitable); a mean near 0
    with lim_higher_frac ≈ 0.5 = symmetric noise."""
    n = len(diffs)
    if n == 0:
        return {"n": 0}
    mean = sum(diffs) / n
    med_abs = _median([abs(d) for d in diffs])
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if n > 1 else None
    return {
        "n": n,
        "mean_log_odds": mean,
        "median_abs_log_odds": med_abs,
        "se": se,
        "lim_higher_frac": sum(1 for d in diffs if d > 0) / n,
        "systematic": bool(se and abs(mean) > 2 * se),
    }


# ── I/O ──────────────────────────────────────────────────────────────────────

async def _limitless_updown(client):
    """Active Limitless crypto Up/Down markets: list of
    (asset, end_ts, up_price, title, slug)."""
    from polybot.asset_direction import asset_of
    out = []
    for page in range(1, 9):
        r = await client.get(f"{_LIM_BASE}/markets/active", params={"page": page, "limit": 25})
        data = (r.json() or {}).get("data") or []
        if not data:
            break
        for m in data:
            t = m.get("title", "")
            prices, ets = m.get("prices"), m.get("expirationTimestamp")
            if "up or down" not in t.lower() or not prices or not ets:
                continue
            a = asset_of(t)
            dur = _duration_seconds(t)
            if a and dur:
                out.append((a, dur, int(ets) // 1000, float(prices[0]), t, m.get("slug")))
    return out


async def _polymarket_updown(gamma):
    """LIVE Polymarket Up/Down crypto markets from gamma (NOT our stale DB, which
    stopped ingesting crypto): (asset, duration, end_ts, up_price, question).
    Filters to windows ending in the next 6h — gamma's active flag also returns
    stale never-closed markets, so a date window is required."""
    import json
    from datetime import datetime, timedelta, timezone

    from polybot.asset_direction import asset_of
    now = datetime.now(tz=timezone.utc)
    mk = await gamma.get("/markets", params={
        "active": "true", "closed": "false", "limit": 500,
        "end_date_min": now.isoformat(),
        "end_date_max": (now + timedelta(hours=6)).isoformat(),
        "order": "endDate", "ascending": "true",
    }) or []
    out = []
    for m in mk:
        q = m.get("question", "") or ""
        if "up or down" not in q.lower():
            continue
        a = asset_of(q)
        dur = _duration_seconds(q)
        end, px = m.get("endDate"), m.get("outcomePrices")
        if not (a and dur and end and px):
            continue
        try:
            prices = json.loads(px) if isinstance(px, str) else px
            up = float(prices[0])
            ets = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError, IndexError):
            continue
        out.append((a, dur, ets, up, q))
    return out


def _match(lim, poly, tol=60):
    """1:1 nearest match by asset + DURATION + resolution time (within ``tol`` s).
    Same asset + duration + end ⇒ same window (start = end − duration), so it's a
    true same-market comparison — not a 15-min vs hourly that merely share an end.

    ``tol`` only resolves sub-minute clock-boundary skew between the two venues'
    resolution timestamps: the shortest window is 5 min (300 s), so distinct
    same-duration windows are ≥300 s apart and any tol < 150 can never confuse
    adjacent windows. Both prices are the venues' LIVE quotes; each Polymarket
    market is used at most once. Returns (asset, lim_up, poly_up, slug, skew_s)
    where skew_s = lim_end − poly_end (how far the venues' resolution times differ)."""
    used: set[int] = set()
    pairs = []
    for la, ld, le, lp, _lt, lslug in sorted(lim, key=lambda x: x[2]):
        best, bi, bestd = None, None, tol + 1
        for i, (pa, pd, pe, pup, _pq) in enumerate(poly):
            if i in used or pa != la or pd != ld:
                continue
            if abs(le - pe) < bestd:
                best, bi, bestd = (pup, le - pe), i, abs(le - pe)
        if best is not None:
            used.add(bi)
            pairs.append((la, lp, best[0], lslug, best[1]))   # asset, lim_up, poly_up, slug, skew_s
    return pairs


async def run(*, loop, interval):
    import httpx
    from polybot.clients import GammaClient
    seen: dict[str, float] = {}     # lim_slug -> latest log-odds diff (distinct-pair sample)
    passes = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=20) as hc:
                lim = await _limitless_updown(hc)
            gamma = GammaClient()
            try:
                poly = await _polymarket_updown(gamma)
            finally:
                await gamma.close()
            pairs = _match(lim, poly)
            rows = []
            for asset, lim_up, poly_up, slug, skew in pairs:
                rd = relative_divergence(lim_up, poly_up)
                if rd is None:
                    continue
                seen[slug] = rd["log_odds_diff"]
                rows.append((asset, lim_up, poly_up, rd, skew))

            print(f"\n[pass {passes}] aligned pairs: {len(rows)}  | distinct sample: {len(seen)}"
                  f"  (lim={len(lim)} poly={len(poly)} candidates)")
            print(f"{'asset':>6} {'LIM':>7} {'POLY':>7} {'ratio':>7} {'log-odds Δ':>11} {'skew_s':>7}")
            for asset, lu, pu, rd, skew in sorted(rows, key=lambda x: -abs(x[3]["log_odds_diff"])):
                print(f"{asset:>6} {lu:>7.3f} {pu:>7.3f} {rd['ratio']:>7.2f} "
                      f"{rd['log_odds_diff']:>+11.3f} {skew:>+7d}")
            summ = divergence_summary(list(seen.values()))
            if summ["n"] >= 1:
                print(f"\nSAMPLE (n={summ['n']} distinct pairs): "
                      f"mean log-odds Δ {summ['mean_log_odds']:+.3f}"
                      + (f" ±2se {2 * summ['se']:.3f}" if summ['se'] else "")
                      + f", median |Δ| {summ['median_abs_log_odds']:.3f}, "
                      f"LIM-higher {summ['lim_higher_frac']:.0%} -> "
                      + ("SYSTEMATIC (one venue consistently richer)" if summ["systematic"]
                         else "symmetric NOISE (no consistent direction)"))
        except Exception:  # noqa: BLE001
            logging.exception("cross_venue pass failed")
        passes += 1
        if not loop:
            break
        await asyncio.sleep(max(10, interval))


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Polymarket vs Limitless relative-divergence scanner")
    ap.add_argument("--loop", action="store_true", help="accumulate distinct pairs over time")
    ap.add_argument("--interval", type=int, default=30, help="seconds between passes in --loop")
    args = ap.parse_args()
    asyncio.run(run(loop=args.loop, interval=args.interval))


if __name__ == "__main__":
    main()
