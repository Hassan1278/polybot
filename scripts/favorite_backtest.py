"""favorite_backtest.py — "just pick the winner" across ALL multi-outcome markets.

The weather thread found the *favorite bucket* of a temperature ladder is underpriced
(+4.3%/$1 gross: priced 0.39, wins 0.43) — the classic multi-outcome **favorite-longshot
bias** (the many cheap longshots are overpriced, the favorite underpriced to compensate).
This generalizes it: every Polymarket multi-candidate event is N sibling YES/NO markets
sharing an `event_id` (election candidates, ladder buckets, …) of which exactly one
resolves YES. Back the highest-priced sibling — *no model, just read the order book* — and
measure realized edge = win-rate − price.

Why this isn't the thread-2 calibration backtest: that one bucketed ALL markets by
absolute price and found them calibrated. It never conditioned on *being the favorite
within an event*; a ladder favorite sits at ~0.40 (mid-range), invisible to a
cheap-vs-dear extremes test. This conditions on rank-within-event — a different cut.

Edge segmented by event size N (the bias should grow with N — more longshots to
overprice) and by category. Price = YES history sampled at `--fraction` of each
sibling's life (siblings share a life, so it's a consistent cross-sibling snapshot);
outcome from our resolved flag, else the terminal price. Throttled + disk-cached so the
big run is rate-limit-free and resumable.

Run on the VPS:
    docker compose exec -T executor python -m scripts.favorite_backtest --days 30 --rate 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
from collections import defaultdict

from scripts.calibration_backtest import (
    _history,
    calibration_table,
    outcome_from_history,
    resolved_yes,
    sample_at_fraction,
)

_CACHE = "/tmp/favorite_px_cache.json"


# ── pure core (unit-tested) ──────────────────────────────────────────────────


def size_bucket(n):
    """Group an event's outcome-count N into a readable bucket."""
    if n <= 2:
        return "2 (binary)"
    if n <= 4:
        return "3-4"
    if n <= 8:
        return "5-8"
    return "9+"


def pick_favorite(sibs):
    """sibs: list of dicts with a numeric 'price'. Return the highest-priced one
    (the market's favorite), or None if empty."""
    sibs = [s for s in sibs if s.get("price") is not None]
    if not sibs:
        return None
    return max(sibs, key=lambda s: s["price"])


def edge_stats(rows):
    """rows: list of (won 0/1, price). Realized edge = mean(won − price), with se,
    win-rate, avg price. {'n':0} if empty."""
    if not rows:
        return {"n": 0}
    e = [w - p for w, p in rows]
    n = len(e)
    mean = sum(e) / n
    var = sum((x - mean) ** 2 for x in e) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if n > 1 else None
    return {"n": n, "edge": mean, "se": se,
            "hit": sum(w for w, _ in rows) / n, "price": sum(p for _, p in rows) / n}


def verdict(s):
    if not s.get("n") or s.get("se") is None:
        return "n/a"
    if s["edge"] - 2 * s["se"] > 0:
        return "+EV"
    if s["edge"] + 2 * s["se"] < 0:
        return "NEGATIVE"
    return "breakeven/noise"


# ── throttle ──────────────────────────────────────────────────────────────────


class _Rate:
    """Serialize request starts to at most `per_sec` per second (monotonic clock)."""

    def __init__(self, per_sec):
        self._interval = 1.0 / max(per_sec, 0.1)
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            if now < self._next:
                await asyncio.sleep(self._next - now)
                now = asyncio.get_event_loop().time()
            self._next = now + self._interval


# ── I/O ──────────────────────────────────────────────────────────────────────


async def _load_events(s, days):
    from datetime import datetime, timedelta, timezone

    from polybot.models import Market
    from sqlalchemy import select
    now = datetime.now(tz=timezone.utc)
    q = (select(Market.market_id, Market.event_id, Market.yes_token_id, Market.outcome,
                Market.outcomes, Market.category, Market.volume_24h_usdc)
         .where(Market.event_id.is_not(None),
                Market.yes_token_id.is_not(None),
                Market.end_date.is_not(None),
                Market.end_date < now,
                Market.end_date >= now - timedelta(days=days))
         .order_by(Market.end_date.desc()))
    return (await s.execute(q)).all()


async def _price_sib(clob, rate, cache, sib, frac, fidelity):
    """Return sib augmented with price (YES at `frac` of life) and won (0/1), or None
    if unusable. Caches the (price, won) pair; transient/no-history is not cached."""
    tok = sib["token"]
    key = f"{tok}:{frac}:{fidelity}"
    if key in cache:
        price, won = cache[key]
    else:
        await rate.wait()
        hist = await _history(clob, tok, fidelity)
        if not hist:                       # transient error OR no history — don't cache
            return None
        pt = sample_at_fraction(hist, frac)
        price = pt[1] if pt else None
        won = resolved_yes(sib["outcome"], sib["outcomes"])
        if won is None:
            won = outcome_from_history(hist)
        cache[key] = (price, won)
    if price is None or won is None:
        return None
    return {**sib, "price": price, "won": won}


async def run(*, days, min_sibs, frac, fidelity, rate, haircuts):
    from polybot.clients import ClobClient
    from polybot.db import session_scope

    async with session_scope() as s:
        rows = await _load_events(s, days)
    events = defaultdict(list)
    for r in rows:
        events[r.event_id].append({
            "token": str(r.yes_token_id), "outcome": r.outcome, "outcomes": r.outcomes,
            "category": r.category, "vol": float(r.volume_24h_usdc or 0)})
    events = {eid: sibs for eid, sibs in events.items() if len(sibs) >= min_sibs}
    n_sib = sum(len(v) for v in events.values())
    print(f"loaded {len(rows)} ended markets -> {len(events)} multi-outcome events "
          f"(>= {min_sibs} siblings), {n_sib} siblings to price @ {rate}/s…")
    if not events:
        print("no multi-outcome events in window")
        return

    cache = {}
    if os.path.exists(_CACHE):
        with open(_CACHE) as f:
            cache = json.load(f)
    rl, clob = _Rate(rate), ClobClient()
    eta = (n_sib - len(cache)) / max(rate, 0.1)
    print(f"  (~{eta / 60:.0f} min for the uncached siblings; progress every 100 events, "
          f"cache checkpointed — safe to Ctrl-C and re-run to resume)", flush=True)

    done = 0

    def _flush():
        with open(_CACHE, "w") as f:
            json.dump(cache, f)

    async def do_event(eid, sibs):
        nonlocal done
        priced = await asyncio.gather(*[_price_sib(clob, rl, cache, x, frac, fidelity) for x in sibs])
        priced = [p for p in priced if p]
        done += 1
        if done % 100 == 0:
            _flush()                       # checkpoint so a kill/resume keeps its work
            print(f"  …{done}/{len(events)} events priced ({len(cache)} cached)", flush=True)
        if len(priced) < 2:               # need a real choice among priced siblings
            return None
        fav = pick_favorite(priced)
        return {"won": float(fav["won"]), "price": fav["price"], "n_out": len(priced),
                "category": fav["category"], "vol": max(p["vol"] for p in priced),
                "sibs": [(p["price"], int(p["won"])) for p in priced]}

    try:
        results = await asyncio.gather(*[do_event(e, s) for e, s in events.items()])
    finally:
        await clob.close()
        _flush()
    favs = [r for r in results if r]
    print(f"priced {len(favs)}/{len(events)} events (>=2 siblings priced)\n")
    if not favs:
        print("nothing priced — check rate limit / history retention")
        return

    def show(name, rs, hc):
        s = edge_stats([(r["won"], r["price"] + hc) for r in rs])
        if not s.get("n"):
            print(f"    {name}: (none)")
            return
        twose = f"±{2 * s['se']:.3f}" if s["se"] else ""
        print(f"    {name}: edge {s['edge']:+.3f}/$1 {twose}  (win {s['hit']:.0%} @ avg price "
              f"{s['price']:.3f}, n={s['n']})  -> {verdict(s)}")

    print("===== PICK THE WINNER — back the favorite sibling per event =====")
    for hc in haircuts:
        print(f"  -- haircut {hc:.2f}/bet (spread; venue has no fee) --")
        show("all        ", favs, hc)

    print("\n===== by event size N (bias should grow with N) — haircut 0.01 =====")
    by_n = defaultdict(list)
    for r in favs:
        by_n[size_bucket(r["n_out"])].append(r)
    for b in ("2 (binary)", "3-4", "5-8", "9+"):
        if by_n.get(b):
            show(f"N={b:<10}", by_n[b], 0.01)

    # both sides of the bias in one curve: YES-edge by price level, conditioned on being
    # a sibling WITHIN a multi-outcome event (the cut thread-2 never made).
    all_samples = [s for r in favs for s in r["sibs"]]
    print("\n===== WITHIN-EVENT CALIBRATION (YES-edge by price level, all siblings) =====")
    print(f"  {'price':>11} {'n':>6} {'mean_px':>8} {'win_rate':>9} {'edge':>8} {'2se':>7}")
    for r in calibration_table(all_samples, n_buckets=10):
        if not r["n"]:
            continue
        sig = " *" if r["se"] and abs(r["edge"]) > 2 * r["se"] else ""
        print(f"  {r['lo']:.1f}-{r['hi']:.1f}  {r['n']:>6} {r['mean_price']:>8.3f} "
              f"{r['yes_rate']:>9.3f} {r['edge']:>+8.3f} {2 * r['se']:>7.3f}{sig}")
    print("  edge>0 (dear end) = underpriced favorites -> BUY YES;  edge<0 (cheap end) = "
          "overpriced longshots -> BUY NO;  '*' = |edge|>2se")

    print("\n===== by category (>=20 events) — haircut 0.01 =====")
    by_cat = defaultdict(list)
    for r in favs:
        by_cat[r["category"] or "?"].append(r)
    for cat, rs in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        if len(rs) >= 20:
            show(f"{cat:<12}", rs, 0.01)


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Back the favorite sibling across all multi-outcome events")
    ap.add_argument("--days", type=int, default=30, help="lookback on market end_date")
    ap.add_argument("--min-sibs", type=int, default=3, help="min outcomes for a 'multi-outcome' event")
    ap.add_argument("--fraction", type=float, default=0.5, help="sample YES price at this fraction of life")
    ap.add_argument("--fidelity", type=int, default=60, help="price-history resolution (minutes)")
    ap.add_argument("--rate", type=float, default=8.0, help="CLOB requests/sec (throttle)")
    args = ap.parse_args()
    asyncio.run(run(days=args.days, min_sibs=args.min_sibs, frac=args.fraction,
                    fidelity=args.fidelity, rate=args.rate, haircuts=(0.0, 0.01, 0.02, 0.03)))


if __name__ == "__main__":
    main()
