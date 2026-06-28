"""cross_venue_divergence.py — EXECUTABLE cross-venue check between Polymarket and
Limitless on the same crypto Up/Down market (matched by asset + duration + the exact
resolution second).

Two things are measured, both from REAL order books (never the venues' seed/quote
fields — Limitless's inline ``prices`` is a 0.49/0.51 placeholder on untraded windows,
not a tradeable quote):

  1. EXECUTABLE CROSS (the arb): the same window's "Up" pays $1 on both venues iff
     the asset is up, so buying Up on the cheap venue + Down on the other locks $1.
     With each venue's binary tight (Up+Down≈1), that reduces to a book cross —
     edge = max(poly_bid_up − lim_ask_up, lim_bid_up − poly_ask_up). edge>0 (net of
     fees+gas) = a real lock; edge≤0 = no arb. This is the headline.
  2. MID DIVERGENCE (the statistical view): log-odds gap between the two book mids,
     and whether it's SYSTEMATIC (one venue consistently richer) or symmetric NOISE.

NOT a finished arb claim even at edge>0: a true lock also needs identical resolution
(both resolve on Chainlink BTC/USD — confirm the SAME stream + timestamps) and the
gas/settlement cost on Base. Observe-only, no keys.

--loop accumulates distinct market-pairs as windows roll. Usage (on the VPS):
    docker compose exec -T executor python -m scripts.cross_venue_divergence
    docker compose run -d --no-deps --name xvenue executor \
        python -m scripts.cross_venue_divergence --loop --interval 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


def best_bid_ask(bids, asks):
    """(best_bid, best_ask) from order-book sides — best bid = highest price, best
    ask = lowest price. Prices may be float (Limitless) or str (Polymarket CLOB);
    malformed levels are skipped. Either side may be None if that side is empty."""
    def _prices(levels):
        out = []
        for lv in levels or []:
            try:
                out.append(float(lv["price"]))
            except (TypeError, ValueError, KeyError):
                continue
        return out
    bp, ap = _prices(bids), _prices(asks)
    return (max(bp) if bp else None, min(ap) if ap else None)


def cross_edge(lim_bid, lim_ask, poly_bid, poly_ask):
    """Executable cross-venue lock edge on a binary that resolves identically on
    both venues, or None if any quote is missing. Buying Up on the cheap venue and
    Down (≈ 1 − the other venue's Up bid) on the other locks $1; profit per $1 =

        edge = max(poly_bid − lim_ask,  lim_bid − poly_ask)

    >0 (net of fees/gas) = a real lock; ≤0 = the books don't cross (no arb)."""
    if None in (lim_bid, lim_ask, poly_bid, poly_ask):
        return None
    a = poly_bid - lim_ask          # buy Up@LIM (ask), Down@POLY (≈1−poly_bid)
    b = lim_bid - poly_ask          # buy Up@POLY (ask), Down@LIM (≈1−lim_bid)
    if a >= b:
        return {"edge": a, "direction": "Up@LIM/Down@POLY"}
    return {"edge": b, "direction": "Up@POLY/Down@LIM"}


def relative_divergence(lim, poly):
    """Relative-divergence metrics between two probabilities (Limitless vs
    Polymarket book mids), or None if either is out of (0,1). ``log_odds_diff`` > 0
    means Limitless is richer; ``ratio`` is lim/poly."""
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
    """Active Limitless crypto Up/Down markets (inline list, no prices — those are
    fetched live from the book): (asset, duration, end_ts, slug, title)."""
    from polybot.asset_direction import asset_of
    out = []
    for page in range(1, 9):
        r = await client.get(f"{_LIM_BASE}/markets/active", params={"page": page, "limit": 25})
        data = (r.json() or {}).get("data") or []
        if not data:
            break
        for m in data:
            t = m.get("title", "")
            ets, slug = m.get("expirationTimestamp"), m.get("slug")
            if "up or down" not in t.lower() or not ets or not slug:
                continue
            a = asset_of(t)
            dur = _duration_seconds(t)
            if a and dur:
                out.append((a, dur, int(ets) // 1000, slug, t))
    return out


async def _limitless_book(client, slug):
    """Live Limitless book for a market: (best_bid, best_ask, mid) or None if the
    book is one-sided/empty (untradeable — exactly the case the seed price hides)."""
    r = await client.get(f"{_LIM_BASE}/markets/{slug}/orderbook")
    d = r.json() or {}
    bid, ask = best_bid_ask(d.get("bids"), d.get("asks"))
    if bid is None or ask is None:
        return None
    mid = d.get("midpoint")
    mid = float(mid) if mid is not None else (bid + ask) / 2.0
    return (bid, ask, mid)


def _up_token(m):
    """Polymarket 'Up'/'Yes' CLOB token id from a gamma market, or None."""
    toks, outs = m.get("clobTokenIds"), m.get("outcomes")
    toks = json.loads(toks) if isinstance(toks, str) else toks
    outs = json.loads(outs) if isinstance(outs, str) else outs
    if not toks:
        return None
    idx = 0
    if isinstance(outs, list):
        for i, o in enumerate(outs):
            if str(o).strip().lower() in ("up", "yes"):
                idx = i
                break
    return str(toks[idx]) if len(toks) > idx else None


async def _polymarket_updown(gamma):
    """LIVE Polymarket Up/Down crypto markets from gamma (NOT our stale DB, which
    stopped ingesting crypto): (asset, duration, end_ts, up_token, question).
    Filters to windows ending in the next 6h — gamma's active flag also returns
    stale never-closed markets, so a date window is required."""
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
        end = m.get("endDate")
        tok = _up_token(m)
        if not (a and dur and end and tok):
            continue
        try:
            ets = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            continue
        out.append((a, dur, ets, tok, q))
    return out


def _match(lim, poly, tol=60):
    """1:1 nearest match by asset + DURATION + resolution time (within ``tol`` s).
    Same asset + duration + end ⇒ same window (start = end − duration), so it's a
    true same-market comparison. ``tol`` only resolves sub-minute clock-boundary
    skew: the shortest window is 5 min (300 s), so distinct same-duration windows
    are ≥300 s apart and any tol < 150 can never confuse adjacent windows. Each
    Polymarket market is used at most once. Returns
    (asset, lim_slug, up_token, end_ts, skew_s) where skew_s = lim_end − poly_end."""
    used: set[int] = set()
    pairs = []
    for la, ld, le, lslug, _lt in sorted(lim, key=lambda x: x[2]):
        best, bi, bestd = None, None, tol + 1
        for i, (pa, pd, pe, ptok, _pq) in enumerate(poly):
            if i in used or pa != la or pd != ld:
                continue
            if abs(le - pe) < bestd:
                best, bi, bestd = (ptok, le - pe), i, abs(le - pe)
        if best is not None:
            used.add(bi)
            pairs.append((la, lslug, best[0], le, best[1]))
    return pairs


async def run(*, loop, interval):
    import httpx
    from polybot.clients import ClobClient, GammaClient
    seen: dict[str, float] = {}     # lim_slug -> latest mid log-odds diff (distinct-pair sample)
    best_arb = None                 # (edge, asset, slug) — best executable cross ever seen
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

                clob = ClobClient()
                rows = []
                try:
                    for asset, lim_slug, up_token, _end, skew in pairs:
                        lb = await _limitless_book(hc, lim_slug)
                        pbk = await clob.book(up_token)
                        if lb is None or not pbk:
                            continue
                        lim_bid, lim_ask, lim_mid = lb
                        poly_bid, poly_ask = best_bid_ask(pbk.get("bids"), pbk.get("asks"))
                        if poly_bid is None or poly_ask is None:
                            continue
                        poly_mid = (poly_bid + poly_ask) / 2.0
                        ce = cross_edge(lim_bid, lim_ask, poly_bid, poly_ask)
                        rd = relative_divergence(lim_mid, poly_mid)
                        if rd is not None:
                            seen[lim_slug] = rd["log_odds_diff"]
                        edge = ce["edge"] if ce else None
                        if edge is not None and (best_arb is None or edge > best_arb[0]):
                            best_arb = (edge, asset, lim_slug)
                        rows.append((asset, lim_mid, poly_mid, lim_ask - lim_bid,
                                     poly_ask - poly_bid, rd, edge, skew))
                finally:
                    await clob.close()

            print(f"\n[pass {passes}] priced pairs: {len(rows)}  | distinct sample: {len(seen)}"
                  f"  (lim={len(lim)} poly={len(poly)} candidates)")
            print(f"{'asset':>6} {'LIMmid':>7} {'POLYmid':>7} {'LIMspr':>7} {'POLYspr':>7} "
                  f"{'midΔ':>7} {'EDGE':>7} {'skew':>5}")
            for asset, lm, pm, lspr, pspr, rd, edge, skew in sorted(
                    rows, key=lambda x: -(x[6] if x[6] is not None else -9)):
                d = f"{rd['log_odds_diff']:+.3f}" if rd else "   —   "
                e = f"{edge:+.3f}" if edge is not None else "   —   "
                print(f"{asset:>6} {lm:>7.3f} {pm:>7.3f} {lspr:>7.3f} {pspr:>7.3f} "
                      f"{d:>7} {e:>7} {skew:>+5d}")

            if best_arb is not None:
                tag = "EXECUTABLE LOCK (verify oracle+gas)" if best_arb[0] > 0 else "no cross (books don't lock)"
                print(f"\nARB: best executable edge so far {best_arb[0]:+.4f}/$1 "
                      f"on {best_arb[1]} ({best_arb[2]}) -> {tag}")
            summ = divergence_summary(list(seen.values()))
            if summ["n"] >= 1:
                print(f"DIVERGENCE (n={summ['n']} distinct pairs, book mids): "
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
    ap = argparse.ArgumentParser(description="Polymarket vs Limitless executable-cross + divergence scanner")
    ap.add_argument("--loop", action="store_true", help="accumulate distinct pairs over time")
    ap.add_argument("--interval", type=int, default=30, help="seconds between passes in --loop")
    args = ap.parse_args()
    asyncio.run(run(loop=args.loop, interval=args.interval))


if __name__ == "__main__":
    main()
