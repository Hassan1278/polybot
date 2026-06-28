"""calibration_backtest.py — is Polymarket mispriced? (favorite-longshot bias)

For each RESOLVED market, sample the YES-token price at a point in its trading life
(default: the midpoint), bucket markets by that price, and compare each bucket's
mean price to the fraction that ACTUALLY resolved YES. A well-calibrated market has
yes_rate ≈ price in every bucket (edge ≈ 0). Systematic gaps ARE the edge:

    edge = yes_rate − price
    edge > 0  ->  that price level is UNDERPRICED   (buying YES there is +EV)
    edge < 0  ->  OVERPRICED                        (fade / sell that side)

The favorite-longshot bias predicts edge < 0 at the cheap end (longshots overpaid
for lottery appeal) and edge > 0 at the expensive end (boring favorites underpaid).
The output curve is the strategy spec: trade the buckets whose edge clears the noise
(|edge| > 2·se). YES side only — the NO side is its mirror.

Price comes from the CLOB prices-history endpoint; the outcome from our resolved
markets table (Market.outcome == outcomes[0] means the YES side won).

Usage (on the VPS, inside the container):
    docker compose exec -T executor python -m scripts.calibration_backtest --limit 400
    ... --fraction 0.5 --buckets 20 --fidelity 60     # horizon, granularity, history res
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math

# polybot/sqlalchemy imports are deferred into the I/O functions below so the pure
# calibration core imports (and unit-tests) with zero env / DB dependency.

# ── pure core (unit-tested) ──────────────────────────────────────────────────


def _norm(s) -> str:
    return str(s).strip().lower() if s is not None else ""


def resolved_yes(outcome, outcomes) -> int | None:
    """1 if the YES outcome (outcomes[0]) won, 0 if a different listed outcome won,
    None if undeterminable. ``outcomes`` is the ordered name list; ``outcome`` the
    winner's name."""
    if not outcomes or outcome is None:
        return None
    yes = _norm(outcomes[0])
    win = _norm(outcome)
    if not yes or not win:
        return None
    if win == yes:
        return 1
    if any(win == _norm(o) for o in outcomes):
        return 0
    return None


def sample_at_fraction(history, frac):
    """``history`` = [(ts, price), ...]; return ``(ts, price)`` at ~``frac`` through
    the trading life (by time), over genuine-uncertainty points only (0 < p < 1), or
    None if there aren't at least two such points."""
    pts = sorted((t, p) for t, p in history if p is not None and 0.0 < p < 1.0)
    if len(pts) < 2:
        return None
    t0, t1 = pts[0][0], pts[-1][0]
    if t1 <= t0:
        return pts[len(pts) // 2]
    target = t0 + frac * (t1 - t0)
    return min(pts, key=lambda tp: abs(tp[0] - target))


def calibration_table(samples, n_buckets=20):
    """``samples`` = [(price, yes01), ...]. Bucket by price over [0,1]; per bucket
    return n, mean_price, yes_rate, edge (=yes_rate−mean_price), se (binomial
    stderr of the yes_rate)."""
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(n_buckets)]
    for price, y in samples:
        if price is None or not (0.0 <= price <= 1.0) or y not in (0, 1):
            continue
        bi = min(int(price * n_buckets), n_buckets - 1)
        buckets[bi].append((price, y))
    table = []
    for i, b in enumerate(buckets):
        row = {"lo": i / n_buckets, "hi": (i + 1) / n_buckets, "n": len(b),
               "mean_price": None, "yes_rate": None, "edge": None, "se": None}
        if b:
            n = len(b)
            mp = sum(p for p, _ in b) / n
            yr = sum(y for _, y in b) / n
            row.update(mean_price=mp, yes_rate=yr, edge=yr - mp,
                       se=math.sqrt(yr * (1.0 - yr) / n))
        table.append(row)
    return table


def bias_summary(table):
    """Aggregate edge at the cheap end (price < 0.2) vs the expensive end
    (price > 0.8) — the favorite-longshot signature. Quality-weighted by bucket n."""
    def agg(rows):
        n = sum(r["n"] for r in rows)
        if not n:
            return {"n": 0, "edge": None}
        e = sum(r["edge"] * r["n"] for r in rows if r["edge"] is not None) / n
        return {"n": n, "edge": e}
    cheap = agg([r for r in table if r["hi"] <= 0.2 and r["n"]])
    dear = agg([r for r in table if r["lo"] >= 0.8 and r["n"]])
    return {"cheap_lt_0.2": cheap, "dear_gt_0.8": dear}


# ── I/O ──────────────────────────────────────────────────────────────────────


async def _load_resolved(s, limit, category):
    from polybot.models import Market
    from sqlalchemy import select
    q = select(Market.market_id, Market.yes_token_id, Market.outcome,
               Market.outcomes, Market.category).where(
        Market.resolved.is_(True),
        Market.yes_token_id.is_not(None),
        Market.outcome.is_not(None),
        Market.outcomes.is_not(None),
        Market.end_date.is_not(None),
    )
    if category:
        q = q.where(Market.category == category)
    q = q.order_by(Market.end_date.desc()).limit(limit)
    return (await s.execute(q)).all()


async def _history(clob, token, fidelity):
    try:
        resp = await clob.price_history(str(token), interval="max", fidelity=fidelity)
    except Exception:  # noqa: BLE001
        return []
    pts = resp.get("history") if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
    out = []
    for pt in pts or []:
        try:
            out.append((int(pt["t"]), float(pt["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def _bounded(factories, limit):
    sem = asyncio.Semaphore(limit)

    async def _run(f):
        async with sem:
            try:
                return await f()
            except Exception:  # noqa: BLE001
                return None

    return await asyncio.gather(*(_run(f) for f in factories))


async def run_backtest(*, limit, category, frac, n_buckets, fidelity, concurrency):
    from polybot.clients import ClobClient
    from polybot.db import session_scope
    async with session_scope() as s:
        rows = await _load_resolved(s, limit, category)
    print(f"loaded {len(rows)} resolved markets"
          f"{f' in {category}' if category else ''}; fetching price history…")
    if not rows:
        print("no resolved markets matched — nothing to do")
        return

    clob = ClobClient()
    try:
        hists = await _bounded([lambda r=r: _history(clob, r[1], fidelity) for r in rows], concurrency)
    finally:
        await clob.close()

    samples = []
    no_history = no_sample = no_outcome = 0
    for r, hist in zip(rows, hists, strict=True):
        if not hist:
            no_history += 1
            continue
        y = resolved_yes(r[2], r[3])
        if y is None:
            no_outcome += 1
            continue
        pt = sample_at_fraction(hist, frac)
        if pt is None:
            no_sample += 1
            continue
        samples.append((pt[1], y))

    print(f"usable samples: {len(samples)}  "
          f"(dropped: no_history={no_history} no_outcome={no_outcome} no_sample={no_sample})")
    if not samples:
        print("0 usable samples — if no_history is high the price-history call/param is wrong; "
              "if no_sample is high, lower --fidelity (short markets need finer history)")
        return

    _print_report(calibration_table(samples, n_buckets), samples, frac)


def _print_report(table, samples, frac):
    base = sum(y for _, y in samples) / len(samples)
    print(f"\nCalibration — {len(samples)} markets, YES price @ {frac:.0%} of life, "
          f"base rate {base:.1%} YES\n")
    print(f"{'bucket':>12} {'n':>6} {'mean_px':>8} {'yes_rate':>9} {'edge':>8} {'2se':>7}")
    print("-" * 56)
    for r in table:
        if not r["n"]:
            continue
        sig = " *" if r["se"] and abs(r["edge"]) > 2 * r["se"] else ""
        bucket = f"{r['lo']:.2f}-{r['hi']:.2f}"
        print(f"{bucket:>12} {r['n']:>6} {r['mean_price']:>8.3f} "
              f"{r['yes_rate']:>9.3f} {r['edge']:>+8.3f} {2 * r['se']:>7.3f}{sig}")
    b = bias_summary(table)
    print("\nfavorite-longshot signature (edge = realized − priced):")
    for k, v in b.items():
        if v["n"]:
            print(f"  {k:>12}: edge {v['edge']:+.3f}  (n={v['n']})")
    print("\nedge>0 = underpriced (buy YES +EV);  edge<0 = overpriced (fade);  '*' = |edge|>2·se")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Polymarket calibration backtest (favorite-longshot bias)")
    ap.add_argument("--limit", type=int, default=400, help="resolved markets to sample (most recent)")
    ap.add_argument("--category", default=None, help="restrict to one category")
    ap.add_argument("--fraction", type=float, default=0.5, help="sample price at this fraction of market life")
    ap.add_argument("--buckets", type=int, default=20)
    ap.add_argument("--fidelity", type=int, default=60, help="price-history resolution (minutes)")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    asyncio.run(run_backtest(limit=args.limit, category=args.category, frac=args.fraction,
                             n_buckets=args.buckets, fidelity=args.fidelity, concurrency=args.concurrency))


if __name__ == "__main__":
    main()
