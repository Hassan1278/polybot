"""crypto_fairvalue_backtest.py — Flavor-2: does Polymarket misprice crypto strikes
vs a model fair value (spot + vol), and does betting toward fair value profit?

For each RESOLVED crypto price-level market ("BTC above $70k by …"), we:
  1. parse (asset, strike K, above/below) — reusing polybot.asset_direction,
  2. sample Polymarket's YES price at a point in the market's life (p_poly),
  3. compute a model fair value P(win) = N(d2) from spot S, strike K, time-to-
     expiry T, and realized vol σ  (a lognormal, zero-drift proxy for the sharp
     options-implied probability — v1; Deribit IV is the v2 upgrade),
  4. read the realized outcome from the terminal Polymarket price.

Then we bin by divergence D = p_fair − p_poly and ask the decisive question: where
the model and Polymarket DISAGREE, does the outcome side with the MODEL (sharper
reference → edge) or with POLYMARKET? The headline is the realized per-share P&L
of betting toward fair value on divergent markets.

This is FLAVOR 2: execution is Polymarket-only; the "second venue" is just the
CEX spot feed used to compute fair value. Observe-only backtest.

Reuses: polybot.asset_direction (parsing), scripts.calibration_backtest (Polymarket
price history + terminal outcome). Spot from Binance klines.

Usage (on the VPS, inside the container):
    docker compose exec -T executor python -m scripts.crypto_fairvalue_backtest --limit 300 --concurrency 3
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math

from scripts.calibration_backtest import _history, outcome_from_history, sample_at_fraction

_YEAR_S = 365.0 * 24.0 * 3600.0
_PPY_HOURLY = 365.0 * 24.0
_SYMBOL = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "DOGE": "DOGEUSDT",
    "XRP": "XRPUSDT", "ADA": "ADAUSDT", "BNB": "BNBUSDT", "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT", "LTC": "LTCUSDT", "DOT": "DOTUSDT", "TRX": "TRXUSDT",
}


# ── pure core (unit-tested) ──────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_prob_above(spot, strike, t_years, sigma):
    """P(S_T > K) under a zero-drift lognormal (r=0): N(d2),
    d2 = [ln(S/K) − ½σ²T] / (σ√T). None on degenerate inputs."""
    if not (spot and strike and t_years and sigma) or spot <= 0 or strike <= 0 \
            or t_years <= 0 or sigma <= 0:
        return None
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * t_years) / (sigma * math.sqrt(t_years))
    return _norm_cdf(d2)


def fair_value(spot, strike, t_years, sigma, is_above):
    """Model P(YES wins): P(S_T>K) for an 'above' market, else P(S_T<K)=1−that."""
    p = bs_prob_above(spot, strike, t_years, sigma)
    if p is None:
        return None
    return p if is_above else 1.0 - p


def realized_vol(prices, periods_per_year=_PPY_HOURLY):
    """Annualized vol from a price series (≥3 positive points): stdev of log
    returns × √periods_per_year. None if too short."""
    px = [p for p in prices if p and p > 0]
    if len(px) < 3:
        return None
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def follow_model_edge(samples, min_div=0.05):
    """Realized per-share P&L of betting TOWARD the model's fair value on markets
    where |p_fair − p_poly| ≥ min_div. Buy YES when fair>poly, NO when fair<poly;
    edge = sign(D)·(outcome − p_poly). >0 means the model beat Polymarket."""
    vals = []
    for pp, pf, y in samples:
        if pp is None or pf is None or y not in (0, 1):
            continue
        d = pf - pp
        if abs(d) < min_div:
            continue
        vals.append((y - pp) if d > 0 else (pp - y))
    if not vals:
        return {"n": 0, "edge": None, "se": None}
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1) if n > 1 else 0.0
    return {"n": n, "edge": mean, "se": math.sqrt(var / n) if n > 1 else None}


def divergence_table(samples, edges=(-0.15, -0.05, 0.05, 0.15)):
    """Bin samples [(p_poly, p_fair, outcome01)] by D = p_fair − p_poly. Per bin:
    n, mean_poly, mean_fair, yes_rate, poly_err=|yes_rate−mean_poly|,
    model_err=|yes_rate−mean_fair|. In the OUTER bins, model_err < poly_err means
    the reference predicts outcomes better than Polymarket where they disagree."""
    bins = [[] for _ in range(len(edges) + 1)]
    for pp, pf, y in samples:
        if pp is None or pf is None or y not in (0, 1):
            continue
        bi = sum(1 for e in edges if (pf - pp) >= e)
        bins[bi].append((pp, pf, y))
    lo_labels = ["-inf", *[f"{e:+.2f}" for e in edges]]
    hi_labels = [*[f"{e:+.2f}" for e in edges], "+inf"]
    out = []
    for i, b in enumerate(bins):
        row = {"bin": f"{lo_labels[i]}..{hi_labels[i]}", "n": len(b),
               "mean_poly": None, "mean_fair": None, "yes_rate": None,
               "poly_err": None, "model_err": None}
        if b:
            n = len(b)
            mp = sum(p for p, _, _ in b) / n
            mf = sum(f for _, f, _ in b) / n
            yr = sum(y for _, _, y in b) / n
            row.update(mean_poly=mp, mean_fair=mf, yes_rate=yr,
                       poly_err=abs(yr - mp), model_err=abs(yr - mf))
        out.append(row)
    return out


# ── I/O ──────────────────────────────────────────────────────────────────────

async def _load_ended_crypto(s, limit):
    from datetime import datetime, timezone

    from polybot.models import Market
    from sqlalchemy import select
    now = datetime.now(tz=timezone.utc)
    rows = (await s.execute(
        select(Market.market_id, Market.yes_token_id, Market.question,
               Market.slug, Market.end_date).where(
            Market.yes_token_id.is_not(None),
            Market.end_date.is_not(None),
            Market.end_date < now,
        ).order_by(Market.end_date.desc()).limit(limit)
    )).all()
    return rows


def _parse_crypto(question, slug):
    """(asset, strike, is_above) for a threshold crypto market, else None — reusing
    asset_direction's precision-over-recall parsers."""
    from polybot.asset_direction import _threshold_price, asset_of, direction
    text = f"{question or ''} {slug or ''}"
    asset = asset_of(text)
    if asset not in _SYMBOL:
        return None
    d = direction(question, slug, "YES", "BUY")        # bull = YES wins above
    if d not in ("bull", "bear"):
        return None
    k = _threshold_price(text)
    if not k or k <= 0:
        return None
    return asset, k, (d == "bull")


async def _bounded(factories, limit):
    sem = asyncio.Semaphore(limit)

    async def _run(f):
        async with sem:
            try:
                return await f()
            except Exception:  # noqa: BLE001
                return None

    return await asyncio.gather(*(_run(f) for f in factories))


async def _spot_history(asset, start_s, end_s):
    """Hourly close prices [(ts_s, price)] for an asset over [start_s, end_s] from
    Binance klines (paginated). [] on failure (e.g. geo-block)."""
    import httpx
    sym = _SYMBOL.get(asset)
    if not sym:
        return []
    out, cur, end_ms = [], int(start_s * 1000), int(end_s * 1000)
    async with httpx.AsyncClient(timeout=25.0) as c:
        while cur < end_ms:
            try:
                r = await c.get("https://api.binance.com/api/v3/klines",
                                params={"symbol": sym, "interval": "1h",
                                        "startTime": cur, "endTime": end_ms, "limit": 1000})
                data = r.json()
            except Exception:  # noqa: BLE001
                break
            if not isinstance(data, list) or not data:
                break
            for k in data:
                try:
                    out.append((int(k[0]) // 1000, float(k[4])))
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
            if len(data) < 1000:
                break
            cur = int(data[-1][0]) + 1
    return out


def _spot_at(spot, ts, max_gap=6 * 3600):
    if not spot:
        return None
    best = min(spot, key=lambda tp: abs(tp[0] - ts))
    return best[1] if abs(best[0] - ts) <= max_gap else None


def _vol_at(spot, ts, window_days):
    lo = ts - window_days * 24 * 3600
    return realized_vol([p for t, p in spot if lo <= t <= ts])


async def run_backtest(*, limit, frac, concurrency, vol_days, min_div):
    from datetime import timezone

    from polybot.clients import ClobClient
    from polybot.db import session_scope
    async with session_scope() as s:
        rows = await _load_ended_crypto(s, limit)

    parsed = []
    for r in rows:
        p = _parse_crypto(r[2], r[3])
        if p:
            end_ts = r[4].replace(tzinfo=timezone.utc).timestamp() if r[4].tzinfo is None else r[4].timestamp()
            parsed.append((r[1], p[0], p[1], p[2], end_ts))      # token, asset, K, is_above, end_ts
    print(f"loaded {len(rows)} ended markets; parsed {len(parsed)} crypto threshold markets")
    if not parsed:
        print("no parseable crypto threshold markets — nothing to do")
        return

    clob = ClobClient()
    try:
        hists = await _bounded([lambda tok=p[0]: _history(clob, tok, 10) for p in parsed], concurrency)
    finally:
        await clob.close()

    # First pass: Polymarket sample price + outcome + sample time.
    rec = []                          # (asset, K, is_above, end_ts, sample_ts, p_poly, outcome)
    no_poly = no_outcome = no_sample = 0
    for p, hist in zip(parsed, hists, strict=True):
        if not hist:
            no_poly += 1
            continue
        y = outcome_from_history(hist)
        if y is None:
            no_outcome += 1
            continue
        pt = sample_at_fraction(hist, frac)
        if pt is None:
            no_sample += 1
            continue
        rec.append((p[1], p[2], p[3], p[4], pt[0], pt[1], y))

    # Spot per asset over the union of sample windows.
    by_asset: dict[str, list] = {}
    for a, *_ in rec:
        by_asset.setdefault(a, [])
    spot: dict[str, list] = {}
    for a in by_asset:
        sts = [x[4] for x in rec if x[0] == a]
        if not sts:
            continue
        spot[a] = await _spot_history(a, min(sts) - vol_days * 24 * 3600 - 3600, max(sts) + 3600)

    samples = []
    no_spot = no_vol = no_fair = 0
    for a, k, is_above, end_ts, sample_ts, p_poly, y in rec:
        spot_px = _spot_at(spot.get(a, []), sample_ts)
        if spot_px is None:
            no_spot += 1
            continue
        sigma = _vol_at(spot.get(a, []), sample_ts, vol_days)
        if sigma is None:
            no_vol += 1
            continue
        t_years = (end_ts - sample_ts) / _YEAR_S
        p_fair = fair_value(spot_px, k, t_years, sigma, is_above)
        if p_fair is None:
            no_fair += 1
            continue
        samples.append((p_poly, p_fair, y))

    print(f"usable samples: {len(samples)}  (dropped: no_poly_hist={no_poly} "
          f"no_outcome={no_outcome} no_sample={no_sample} no_spot={no_spot} "
          f"no_vol={no_vol} no_fair={no_fair})")
    if not samples:
        print("0 usable samples — if no_spot is high the Binance feed is blocked from "
              "this host (try a different spot source); if no_poly_hist is high, lower --concurrency")
        return

    _print_report(samples, frac, min_div)


def _print_report(samples, frac, min_div):
    print(f"\nCrypto fair-value vs Polymarket — {len(samples)} markets, "
          f"sampled @ {frac:.0%} of life\n")
    print(f"{'divergence bin':>16} {'n':>5} {'poly':>7} {'fair':>7} {'yes_rate':>9} "
          f"{'poly_err':>9} {'model_err':>10}")
    print("-" * 70)
    for r in divergence_table(samples):
        if not r["n"]:
            continue
        print(f"{r['bin']:>16} {r['n']:>5} {r['mean_poly']:>7.3f} {r['mean_fair']:>7.3f} "
              f"{r['yes_rate']:>9.3f} {r['poly_err']:>9.3f} {r['model_err']:>10.3f}")
    fe = follow_model_edge(samples, min_div)
    print(f"\nbetting TOWARD fair value when |divergence| ≥ {min_div:.2f}:")
    if fe["n"]:
        twose = 2 * fe["se"] if fe["se"] else 0.0
        verdict = "EDGE" if fe["edge"] - twose > 0 else ("noise" if abs(fe["edge"]) < twose else "NEGATIVE")
        print(f"  realized edge {fe['edge']:+.4f}/share over n={fe['n']}  (±2se {twose:.4f}) -> {verdict}")
    else:
        print("  (no markets cleared the divergence threshold)")
    print("\nmodel_err < poly_err in the outer bins = the spot/vol reference predicts "
          "outcomes better than Polymarket where they disagree (the edge).")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Polymarket crypto-strike fair-value backtest (Flavor 2)")
    ap.add_argument("--limit", type=int, default=300, help="ended markets to scan (most recent)")
    ap.add_argument("--fraction", type=float, default=0.5, help="sample Polymarket price at this fraction of life")
    ap.add_argument("--concurrency", type=int, default=3, help="parallel price-history fetches (prices-history is rate-limited)")
    ap.add_argument("--vol-days", type=int, default=14, help="trailing window (days) for realized vol")
    ap.add_argument("--min-div", type=float, default=0.05, help="divergence threshold for the follow-model edge")
    args = ap.parse_args()
    asyncio.run(run_backtest(limit=args.limit, frac=args.fraction, concurrency=args.concurrency,
                             vol_days=args.vol_days, min_div=args.min_div))


if __name__ == "__main__":
    main()
