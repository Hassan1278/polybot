"""weather_market_test.py — STEP 2c: the decisive money test.

For every resolved highest-temp ladder: take ICON's 24h-lead forecast (optionally +bias
for the measured ~0.5° WU-hot offset), map it to the bucket it points at, BUY that bucket
at its market price ~24h before resolution (CLOB price history), and settle by the actual
outcome. Realized edge = mean(hit − price):
  • > 0 net of fees  → the forecast beats the market (it's money);
  • ≈ 0              → the market already prices the forecast (efficient);
  • < 0              → the market beats the forecast.
Segmented by volume — a residual edge is likeliest in the illiquid long-tail. The price
used is the historical mid/last (ignores spread), so a positive edge is an upper bound.

Run on the VPS:
    docker compose exec -T executor python -m scripts.weather_market_test --days 14
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math

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
from scripts.weather_truth import reconstruct

_OM = "https://previous-runs-api.open-meteo.com/v1/forecast"
_ICON_24H = "temperature_2m_previous_day1_icon_seamless"   # forecast known 24h before


def match_bucket(val, buckets):
    """buckets: list of (label, parsed_c). Return the label whose range CONTAINS val
    (narrowest wins, so a 1° point bucket beats an open-ended one), else nearest by
    midpoint. None if empty or val is None."""
    if val is None or not buckets:
        return None
    contain = [(lbl, pb) for lbl, pb in buckets if pb["lo"] <= val <= pb["hi"]]
    if contain:
        return min(contain, key=lambda lp: lp[1]["hi"] - lp[1]["lo"])[0]
    return min(buckets, key=lambda lp: abs(lp[1]["mid"] - val))[0]


def summarize_edge(rows):
    """rows: list of (hit 0/1, price). Realized edge = mean(hit − price), with se,
    hit-rate, avg price, and a significance flag (|edge| > 2·se)."""
    if not rows:
        return {"n": 0}
    e = [h - p for h, p in rows]
    n = len(e)
    mean = sum(e) / n
    var = sum((x - mean) ** 2 for x in e) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if n > 1 else None
    return {"n": n, "edge": mean, "se": se, "hit": sum(h for h, _ in rows) / n,
            "price": sum(p for _, p in rows) / n,
            "sig": bool(se and abs(mean) > 2 * se)}


def _verdict(s):
    if not s.get("n") or s.get("se") is None:
        return "n/a"
    if s["edge"] - 2 * s["se"] > 0:
        return "EDGE (forecast beats market)"
    if s["edge"] + 2 * s["se"] < 0:
        return "NEGATIVE (market beats forecast)"
    return "efficient / noise (no edge)"


async def run(*, days, cap, conc, chunk, px_conc, bias, hours_before):
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
        buckets, tok_of, actual, end_ts = [], {}, None, None
        for leg in rec["legs"]:
            pb = _bucket_c(leg["parsed"])
            buckets.append((leg["bucket"], pb))
            tok_of[leg["bucket"]] = leg.get("tok")
            if leg["yes"]:
                actual = leg["bucket"]
            end_ts = end_ts or leg.get("end_ts")
        vol = max((leg.get("vol") or 0) for leg in rec["legs"])
        if actual and end_ts:
            ladders.append({"city": city, "iso": iso, "buckets": buckets, "tok_of": tok_of,
                            "actual": actual, "end_ts": end_ts, "vol": vol})
    if not ladders:
        print("no gradeable highest-temp ladders")
        return
    print(f"{len(ladders)} highest-temp ladders with coords; pulling forecasts + market prices…")

    # --- forecasts: one multi-location call per station-chunk over the window ---
    isos = sorted({lad["iso"] for lad in ladders})
    start, end = isos[0], isos[-1]
    stations = [(c, *STATION_COORDS[c]) for c in sorted({lad["city"] for lad in ladders})]
    pr = {}
    async with httpx.AsyncClient(timeout=90) as client:
        for i in range(0, len(stations), chunk):
            grp = stations[i:i + chunk]
            d = await _get(client, _OM, {
                "latitude": ",".join(f"{s[1]}" for s in grp),
                "longitude": ",".join(f"{s[2]}" for s in grp),
                "start_date": start, "end_date": end, "hourly": _HOURLY,
                "models": _MODELS, "timezone": "auto", "temperature_unit": "celsius"})
            for j, (c, _la, _lo) in enumerate(grp):
                pj = _nth(d, j)
                pr[c] = pj.get("hourly", {}) if pj else {}

    for lad in ladders:
        h = pr.get(lad["city"], {})
        fc = _agg_by_date(h.get("time", []), h.get(_ICON_24H), lad["iso"], "highest")
        lad["bucket_raw"] = match_bucket(fc, lad["buckets"])
        lad["bucket_corr"] = match_bucket(fc + bias if fc is not None else None, lad["buckets"])

    # --- market prices for the forecast bucket(s), ~hours_before before resolution ---
    clob = ClobClient()
    sem = asyncio.Semaphore(px_conc)

    async def price(tok, target_ts):
        if not tok:
            return None
        async with sem:
            try:
                raw = await clob.price_history(tok, interval="max", fidelity=60)
            except Exception:  # noqa: BLE001
                return None
        hist = raw.get("history", []) if isinstance(raw, dict) else (raw or [])
        p, _ts = _sample_at(hist, target_ts)
        return p

    async def price_for(lad, which):
        lbl = lad.get(which)
        tok = lad["tok_of"].get(lbl) if lbl else None
        return await price(tok, lad["end_ts"] - hours_before * 3600)

    try:
        raw_p = await asyncio.gather(*[price_for(lad, "bucket_raw") for lad in ladders])
        corr_p = await asyncio.gather(*[price_for(lad, "bucket_corr") for lad in ladders])
    finally:
        await clob.close()

    def rows_for(which, prices):
        out = []
        for lad, p in zip(ladders, prices, strict=True):
            lbl = lad.get(which)
            if lbl is None or p is None:
                continue
            out.append((1.0 if lbl == lad["actual"] else 0.0, p, lad["vol"]))
        return out

    raw = rows_for("bucket_raw", raw_p)
    corr = rows_for("bucket_corr", corr_p)
    vols = sorted(v for _h, _p, v in raw)
    med = vols[len(vols) // 2] if vols else 0.0

    def show(name, rows):
        s = summarize_edge([(h, p) for h, p, _v in rows])
        if not s.get("n"):
            print(f"  {name}: (no data)")
            return
        twose = f"±{2 * s['se']:.3f}" if s["se"] else ""
        print(f"  {name}: edge {s['edge']:+.3f}/$1 {twose}  (hit {s['hit']:.0%} @ avg price "
              f"{s['price']:.3f}, n={s['n']})  -> {_verdict(s)}")
        lo = [(h, p) for h, p, v in rows if v < med]
        hi = [(h, p) for h, p, v in rows if v >= med]
        sl, sh = summarize_edge(lo), summarize_edge(hi)
        if sl.get("n") and sh.get("n"):
            print(f"      by volume:  low (<${med:,.0f}) edge {sl['edge']:+.3f} (n={sl['n']})   "
                  f"high edge {sh['edge']:+.3f} (n={sh['n']})")

    print("\n===== MONEY TEST — bet ICON's forecast bucket at the 24h market price =====")
    print(f"(bias-correction = +{bias}°, sample {hours_before}h before resolution; "
          "edge = mean(settle − price), spread ignored)")
    show("RAW ICON 24h ", raw)
    show("ICON + bias  ", corr)


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Step 2c: does the forecast bucket beat the market price?")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--cap", type=int, default=0)
    ap.add_argument("--conc", type=int, default=20, help="gamma resolution concurrency")
    ap.add_argument("--chunk", type=int, default=15, help="stations per Open-Meteo call")
    ap.add_argument("--px-conc", type=int, default=8, help="concurrent CLOB price-history calls")
    ap.add_argument("--bias", type=float, default=0.4, help="°C added to ICON (the WU-hot correction)")
    ap.add_argument("--hours-before", type=int, default=24, help="how long before resolution to price")
    args = ap.parse_args()
    asyncio.run(run(days=args.days, cap=args.cap, conc=args.conc, chunk=args.chunk,
                    px_conc=args.px_conc, bias=args.bias, hours_before=args.hours_before))


if __name__ == "__main__":
    main()
