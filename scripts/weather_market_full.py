"""weather_market_full.py — STEP 2c FULL: the comprehensive money test over EVERY bucket.

Prices ALL bucket-markets (not one per ladder) so we can compare the forecast's full
probability distribution to the market's, bucket by bucket. Rate-limiting is beaten with
a deliberate throttle (--rate req/s) plus a disk cache (reruns are instant; an interrupted
run resumes). ICON's 24h forecast (+bias) becomes a Gaussian over each ladder (σ from the
measured forecast error); for every bucket we have market price P_mkt, forecast prob P_fc,
and the actual outcome.

Two verdicts:
  • CALIBRATION (Brier, lower=better): is the forecast distribution a better predictor of
    outcomes than the market's prices?
  • EDGE: bet every bucket the forecast thinks is underpriced (P_fc − P_mkt > min); realized
    edge = mean(settle − price − haircut), segmented by volume.

Run on the VPS:
    docker compose exec -T executor python -m scripts.weather_market_full --days 14 --rate 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os

from scripts.weather_grade import (
    _HOURLY,
    _MODELS,
    STATION_COORDS,
    _agg_by_date,
    _bucket_c,
    _get,
    _nth,
    to_iso,
)
from scripts.weather_market_probe import _sample_at
from scripts.weather_market_test import summarize_edge
from scripts.weather_truth import reconstruct

_OM = "https://previous-runs-api.open-meteo.com/v1/forecast"
_ICON_24H = "temperature_2m_previous_day1_icon_seamless"
_CACHE = "/tmp/weather_px_cache.json"


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def gauss_bucket_prob(mu, sigma, bucket):
    """P(forecast lands in bucket) under N(mu, sigma) — the forecast's implied prob for
    that bucket. Handles open-ended buckets. None if mu missing or sigma ≤ 0."""
    if mu is None or sigma <= 0:
        return None
    lo, hi, mid = bucket["lo"], bucket["hi"], bucket["mid"]
    if bucket.get("open"):
        # the *real* bound is the one nearest the midpoint; the other is padding
        if abs(hi - mid) <= abs(lo - mid):        # "X or below" → (−inf, hi]
            return _ncdf((hi - mu) / sigma)
        return 1.0 - _ncdf((lo - mu) / sigma)     # "X or higher" → [lo, +inf)
    return _ncdf((hi - mu) / sigma) - _ncdf((lo - mu) / sigma)


def brier(rows, key):
    """Mean squared error of probabilities vs outcomes: mean((p − won)^2) over rows that
    have both p (rows[i][key]) and won. Lower is a better forecaster."""
    vals = [(r[key] - r["won"]) ** 2 for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


class _Rate:
    """Serializes request starts to at most `per_sec` per second (monotonic clock)."""

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


async def _price(clob, rate, cache, tok, target_ts):
    if not tok:
        return None
    key = f"{tok}:{target_ts // 3600}"
    if key in cache:
        return cache[key]
    await rate.wait()
    try:
        raw = await clob.price_history(tok, interval="max", fidelity=60)
    except Exception:  # noqa: BLE001
        cache[key] = None
        return None
    hist = raw.get("history", []) if isinstance(raw, dict) else (raw or [])
    p, _ts = _sample_at(hist, target_ts)
    cache[key] = p
    return p


async def run(*, days, cap, conc, chunk, bias, sigma, hours_before, haircut, rate, edge_min):
    import httpx
    from polybot.clients import ClobClient

    clean, _none, _multi = await reconstruct(days=days, cap=cap, conc=conc)
    ladders = []
    for rec in clean:
        city, date, kind = rec["key"]
        if kind != "highest" or city not in STATION_COORDS:
            continue
        iso = to_iso(date)
        if not iso:
            continue
        buckets, end_ts = [], None
        for leg in rec["legs"]:
            buckets.append((leg["bucket"], _bucket_c(leg["parsed"]), leg.get("tok"), bool(leg["yes"])))
            end_ts = end_ts or leg.get("end_ts")
        vol = max((leg.get("vol") or 0) for leg in rec["legs"])
        if end_ts and any(b[3] for b in buckets):
            ladders.append({"city": city, "iso": iso, "buckets": buckets, "end_ts": end_ts, "vol": vol})
    if not ladders:
        print("no gradeable ladders")
        return

    # --- forecasts (batched multi-location) → mu per ladder ---
    isos = sorted({lad["iso"] for lad in ladders})
    stations = [(c, *STATION_COORDS[c]) for c in sorted({lad["city"] for lad in ladders})]
    pr = {}
    async with httpx.AsyncClient(timeout=90) as client:
        for i in range(0, len(stations), chunk):
            grp = stations[i:i + chunk]
            d = await _get(client, _OM, {
                "latitude": ",".join(f"{s[1]}" for s in grp),
                "longitude": ",".join(f"{s[2]}" for s in grp),
                "start_date": isos[0], "end_date": isos[-1], "hourly": _HOURLY,
                "models": _MODELS, "timezone": "auto", "temperature_unit": "celsius"})
            for j, (c, _la, _lo) in enumerate(grp):
                pj = _nth(d, j)
                pr[c] = pj.get("hourly", {}) if pj else {}
    for lad in ladders:
        h = pr.get(lad["city"], {})
        fc = _agg_by_date(h.get("time", []), h.get(_ICON_24H), lad["iso"], "highest")
        lad["mu"] = (fc + bias) if fc is not None else None

    # --- price EVERY bucket (throttled + cached) ---
    cache = {}
    if os.path.exists(_CACHE):
        with open(_CACHE) as _f:
            cache = json.load(_f)
    rl = _Rate(rate)
    clob = ClobClient()
    n_buckets = sum(len(lad["buckets"]) for lad in ladders)
    print(f"pricing {n_buckets} buckets across {len(ladders)} ladders @ {rate}/s "
          f"(cached: {len(cache)}); first run is slow, reruns instant…")

    rows = []

    async def fill(lad):
        tgt = lad["end_ts"] - hours_before * 3600
        for label, pb, tok, is_yes in lad["buckets"]:
            p = await _price(clob, rl, cache, tok, tgt)
            rows.append({"label": label, "p_mkt": p, "won": 1.0 if is_yes else 0.0,
                         "p_fc": gauss_bucket_prob(lad["mu"], sigma, pb), "vol": lad["vol"]})

    try:
        await asyncio.gather(*[fill(lad) for lad in ladders])
    finally:
        await clob.close()
        with open(_CACHE, "w") as _f:
            json.dump(cache, _f)

    priced = [r for r in rows if r["p_mkt"] is not None]
    both = [r for r in priced if r["p_fc"] is not None]
    print(f"\npriced {len(priced)}/{len(rows)} buckets ({len(both)} with a forecast prob)")

    # --- calibration ---
    bf, bm = brier(both, "p_fc"), brier(both, "p_mkt")
    print("\n===== CALIBRATION (Brier score, lower = better predictor) =====")
    if bf is not None and bm is not None:
        better = "FORECAST better" if bf < bm else "MARKET better"
        print(f"  forecast {bf:.4f}   vs   market {bm:.4f}   over n={len(both)}  -> {better}")

    # --- edge: bet every bucket the forecast thinks is underpriced ---
    bets = [r for r in both if (r["p_fc"] - r["p_mkt"]) > edge_min]
    vols = sorted(r["vol"] for r in bets)
    med = vols[len(vols) // 2] if vols else 0.0

    def show_edge(name, rs):
        s = summarize_edge([(r["won"], r["p_mkt"] + haircut) for r in rs])
        if not s.get("n"):
            print(f"  {name}: (no bets)")
            return
        twose = f"±{2 * s['se']:.3f}" if s["se"] else ""
        verdict = ("EDGE" if s["se"] and s["edge"] - 2 * s["se"] > 0
                   else "NEGATIVE" if s["se"] and s["edge"] + 2 * s["se"] < 0
                   else "efficient/noise")
        print(f"  {name}: edge {s['edge']:+.3f}/$1 {twose}  (hit {s['hit']:.0%} @ avg cost "
              f"{s['price']:.3f}, n={s['n']})  -> {verdict}")

    print(f"\n===== EDGE — bet buckets where forecast prob > market by {edge_min} "
          f"(haircut {haircut:.3f}/bet) =====")
    show_edge("all       ", bets)
    show_edge("low-volume", [r for r in bets if r["vol"] < med])
    show_edge("high-volume", [r for r in bets if r["vol"] >= med])


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Step 2c FULL: forecast distribution vs market over every bucket")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--cap", type=int, default=0)
    ap.add_argument("--conc", type=int, default=20, help="gamma resolution concurrency")
    ap.add_argument("--chunk", type=int, default=15, help="stations per Open-Meteo call")
    ap.add_argument("--bias", type=float, default=0.4, help="°C added to ICON (WU-hot correction)")
    ap.add_argument("--sigma", type=float, default=1.4, help="forecast σ (°C) for the Gaussian")
    ap.add_argument("--hours-before", type=int, default=24)
    ap.add_argument("--haircut", type=float, default=0.0, help="cost/bet for spread+fees")
    ap.add_argument("--rate", type=float, default=8.0, help="CLOB requests/sec (throttle)")
    ap.add_argument("--edge-min", type=float, default=0.05, help="min forecast−market gap to bet")
    args = ap.parse_args()
    asyncio.run(run(days=args.days, cap=args.cap, conc=args.conc, chunk=args.chunk, bias=args.bias,
                    sigma=args.sigma, hours_before=args.hours_before, haircut=args.haircut,
                    rate=args.rate, edge_min=args.edge_min))


if __name__ == "__main__":
    main()
