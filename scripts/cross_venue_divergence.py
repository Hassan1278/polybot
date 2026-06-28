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

_LIM_BASE = "https://api.limitless.exchange"


# ── pure core (unit-tested) ──────────────────────────────────────────────────

def logit(p):
    """Log-odds of a probability, clamped off the 0/1 boundary."""
    p = min(max(float(p), 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


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
            if a:
                out.append((a, int(ets) // 1000, float(prices[0]), t, m.get("slug")))
    return out


async def _polymarket_updown(s):
    """Active Polymarket Up/Down markets: list of (asset, end_ts, yes_token, question)."""
    from datetime import datetime, timezone

    from polybot.asset_direction import asset_of
    from polybot.models import Market
    from sqlalchemy import select
    now = datetime.now(tz=timezone.utc)
    rows = (await s.execute(
        select(Market.question, Market.slug, Market.yes_token_id, Market.end_date).where(
            Market.resolved.is_(False), Market.end_date > now,
            Market.question.ilike("%up or down%"), Market.yes_token_id.is_not(None),
        )
    )).all()
    out = []
    for q, sl, tok, ed in rows:
        a = asset_of(f"{q} {sl}")
        if not a or not ed:
            continue
        ts = int(ed.replace(tzinfo=timezone.utc).timestamp()) if ed.tzinfo is None else int(ed.timestamp())
        out.append((a, ts, str(tok), q))
    return out


def _match(lim, poly, tol=120):
    """1:1 nearest match by asset + resolution time (within ``tol`` seconds).
    Each Limitless market maps to its single closest Polymarket counterpart."""
    pairs = []
    for la, le, lp, lt, lslug in lim:
        best, bestd = None, tol + 1
        for pa, pe, ptok, pq in poly:
            if pa == la and abs(le - pe) < bestd:
                best, bestd = (pa, pe, ptok, pq), abs(le - pe)
        if best:
            pairs.append((la, lp, lslug, lt, best[2], best[3]))   # asset, lim_up, slug, ltitle, ptok, pq
    return pairs


async def run(*, loop, interval):
    import httpx
    from polybot.clients import ClobClient
    from polybot.db import session_scope
    seen: dict[str, float] = {}     # lim_slug -> latest log-odds diff (distinct-pair sample)
    passes = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=20) as hc:
                lim = await _limitless_updown(hc)
            async with session_scope() as s:
                poly = await _polymarket_updown(s)
            pairs = _match(lim, poly)
            clob = ClobClient()
            rows = []
            try:
                for asset, lim_up, slug, _ltitle, ptok, _pq in pairs:
                    poly_up = float(await clob.midpoint(ptok) or 0.0)
                    rd = relative_divergence(lim_up, poly_up)
                    if rd is None:
                        continue
                    seen[slug] = rd["log_odds_diff"]
                    rows.append((asset, lim_up, poly_up, rd))
            finally:
                await clob.close()

            print(f"\n[pass {passes}] aligned pairs: {len(rows)}  | distinct sample: {len(seen)}")
            print(f"{'asset':>6} {'LIM':>7} {'POLY':>7} {'ratio':>7} {'log-odds Δ':>11}")
            for asset, lu, pu, rd in sorted(rows, key=lambda x: -abs(x[3]["log_odds_diff"])):
                print(f"{asset:>6} {lu:>7.3f} {pu:>7.3f} {rd['ratio']:>7.2f} {rd['log_odds_diff']:>+11.3f}")
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
