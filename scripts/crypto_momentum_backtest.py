"""crypto_momentum_backtest.py — do Polymarket "X Up or Down" crowds overreact?

The abundant crypto instrument on Polymarket is the short-window momentum market
("Bitcoin Up or Down, 8:00–8:15AM") — no strike, fair value ≈ 0.50 (a short move is
~a martingale). The behavioral hypothesis: prediction crowds CHASE momentum, so
after a run they overprice continuation. We test it by betting TOWARD 0.50 (fading
the crowd's lean) and measuring realized P&L over a large sample.

For each resolved Up/Down market: sample Polymarket's "Up" (YES) price, read the
outcome from the terminal price, pair with fair = 0.50. follow_model_edge then says
whether fading the lean profits, and divergence_table shows whether leans over- or
under-shoot. Polymarket-only (Flavor 2); the "reference" is just the martingale 0.50.

Reuses scripts.crypto_fairvalue_backtest (follow_model_edge, divergence_table, the
bounded fetcher) and scripts.calibration_backtest (price history, terminal outcome).
Observe-only.

Usage (on the VPS, inside the container):
    docker compose exec -T executor python -m scripts.crypto_momentum_backtest --limit 2000 --concurrency 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from scripts.calibration_backtest import _history, outcome_from_history, sample_at_fraction
from scripts.crypto_fairvalue_backtest import _bounded, divergence_table, follow_model_edge

_FAIR = 0.5     # a short-window move is ~a martingale: P(up) ≈ 0.50


async def _load_updown(s, limit):
    from datetime import datetime, timezone

    from polybot.models import Market
    from sqlalchemy import select
    now = datetime.now(tz=timezone.utc)
    rows = (await s.execute(
        select(Market.market_id, Market.yes_token_id).where(
            Market.yes_token_id.is_not(None),
            Market.end_date.is_not(None),
            Market.end_date < now,
            Market.question.ilike("%up or down%"),
        ).order_by(Market.end_date.desc()).limit(limit)
    )).all()
    return rows


async def run_backtest(*, limit, frac, concurrency, fidelity, min_div):
    from polybot.clients import ClobClient
    from polybot.db import session_scope
    async with session_scope() as s:
        rows = await _load_updown(s, limit)
    print(f"loaded {len(rows)} Up/Down markets; fetching price history…")
    if not rows:
        print("no Up/Down markets found")
        return

    clob = ClobClient()
    try:
        hists = await _bounded([lambda tok=r[1]: _history(clob, tok, fidelity) for r in rows], concurrency)
    finally:
        await clob.close()

    samples = []
    no_hist = no_outcome = no_sample = 0
    for _r, hist in zip(rows, hists, strict=True):
        if not hist:
            no_hist += 1
            continue
        y = outcome_from_history(hist)
        if y is None:
            no_outcome += 1
            continue
        pt = sample_at_fraction(hist, frac)
        if pt is None:
            no_sample += 1
            continue
        samples.append((pt[1], _FAIR, y))

    print(f"usable samples: {len(samples)}  "
          f"(dropped: no_hist={no_hist} no_outcome={no_outcome} no_sample={no_sample})")
    if not samples:
        print("0 usable samples — these markets are very short; if no_sample is high try --fidelity 1")
        return

    _print_report(samples, frac, min_div)


def _print_report(samples, frac, min_div):
    up = sum(y for _, _, y in samples) / len(samples)
    print(f"\nUp/Down momentum — {len(samples)} markets, 'Up' price @ {frac:.0%} of life, "
          f"base rate {up:.1%} UP\n")
    print(f"{'crowd lean bin':>16} {'n':>6} {'poly':>7} {'up_rate':>8} {'poly_err(signed)':>17}")
    print("-" * 60)
    for r in divergence_table(samples):
        if not r["n"]:
            continue
        signed = r["yes_rate"] - r["mean_poly"]      # >0 poly underpriced Up, <0 overpriced Up
        print(f"{r['bin']:>16} {r['n']:>6} {r['mean_poly']:>7.3f} {r['yes_rate']:>8.3f} {signed:>+17.3f}")
    fe = follow_model_edge(samples, min_div)
    print(f"\nfading the crowd's lean toward 0.50 (|lean−0.50| ≥ {min_div:.2f}):")
    if fe["n"]:
        twose = 2 * fe["se"] if fe["se"] else 0.0
        verdict = "EDGE" if fe["edge"] - twose > 0 else ("noise" if abs(fe["edge"]) < twose else "NEGATIVE")
        print(f"  realized edge {fe['edge']:+.4f}/share over n={fe['n']}  (±2se {twose:.4f}) -> {verdict}")
    else:
        print("  (no markets leaned past the threshold)")
    print("\nedge>0 = fading the lean profits (crowd overreacts);  edge<0 = momentum continues (don't fade).")
    print("note: the bins are by D = 0.50 − poly, so the -0.xx bins are UP-leans (poly>0.5).")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Polymarket crypto Up/Down momentum-fade backtest")
    ap.add_argument("--limit", type=int, default=2000, help="Up/Down markets to scan (most recent)")
    ap.add_argument("--fraction", type=float, default=0.5, help="sample the 'Up' price at this fraction of life")
    ap.add_argument("--concurrency", type=int, default=3, help="parallel price-history fetches (rate-limited)")
    ap.add_argument("--fidelity", type=int, default=1, help="price-history resolution (minutes); these markets are short")
    ap.add_argument("--min-div", type=float, default=0.05, help="how far past 0.50 the crowd must lean to count")
    args = ap.parse_args()
    asyncio.run(run_backtest(limit=args.limit, frac=args.fraction, concurrency=args.concurrency,
                             fidelity=args.fidelity, min_div=args.min_div))


if __name__ == "__main__":
    main()
