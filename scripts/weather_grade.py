"""weather_grade.py — STEP 2b-2: grade the forecasts' MARGIN OF FAILURE against the
gamma ground truth, per model and lead time. No model-building — we're scoring existing
professional forecasts against the value that actually settled each market.

For every clean city-day high (from weather_truth.reconstruct), use Open-Meteo's
lead-time forecasts (Previous Runs API: 24h & 48h ahead, plus the latest run) at the
market's RESOLUTION-STATION coordinates, and compute:
  • per model × lead: MAE, signed bias, and hit-rate (forecast lands in the YES bucket);
  • per city: which locations the forecasts miss most;
  • the systematic-bias check — does the settled high run consistently hotter/cooler
    than the forecast (e.g. the Wunderground airport-max-runs-hot effect)?

DATA FIDELITY: also pulls ERA5 reanalysis (an independent "what actually happened") and
compares it to the WU resolution — isolating whether the WU number itself is a faithful,
bettable representation of reality or an idiosyncratic source that caps precision.

Rate-limit-safe: one multi-location call per station-chunk over the whole window (NOT
one per city-day). Everything is normalized to °C. STATION_COORDS are the resolution
airports from the step-1 gamma descriptions — verify/extend; unmapped cities are skipped.
Run on the VPS:
    docker compose exec -T executor python -m scripts.weather_grade --days 14
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import defaultdict

from scripts.weather_truth import reconstruct

_OM = "https://previous-runs-api.open-meteo.com/v1/forecast"
_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"   # ERA5 reanalysis "actual"
_HOURLY = ["temperature_2m", "temperature_2m_previous_day1", "temperature_2m_previous_day2"]
_MODELS = ["icon_seamless", "gfs_seamless", "ecmwf_ifs025"]
_LEADS = [("now", ""), ("24h", "_previous_day1"), ("48h", "_previous_day2")]

# city -> (lat, lon) of the RESOLUTION station (airport unless noted), from the step-1
# gamma descriptions. Approximate; the per-city base-MAE diagnostic flags bad coords.
STATION_COORDS = {
    "New York City": (40.777, -73.873), "Miami": (25.793, -80.290),
    "Atlanta": (33.640, -84.427), "Dallas": (32.847, -96.852),
    "Los Angeles": (33.942, -118.408), "San Francisco": (37.619, -122.375),
    "Chicago": (41.978, -87.905), "Denver": (39.862, -104.673),
    "Austin": (30.195, -97.670), "Seattle": (47.449, -122.309),
    "London": (51.505, 0.055), "Paris": (48.969, 2.441),
    "Madrid": (40.472, -3.561), "Munich": (48.353, 11.786),
    "Amsterdam": (52.309, 4.764), "Warsaw": (52.166, 20.967),
    "Helsinki": (60.317, 24.963), "Milan": (45.630, 8.728),
    "Istanbul": (41.262, 28.742), "Ankara": (40.128, 32.995),
    "Hong Kong": (22.302, 114.174), "Singapore": (1.359, 103.989),
    "Seoul": (37.469, 126.451), "Busan": (35.179, 128.938),
    "Shanghai": (31.143, 121.805), "Shenzhen": (22.639, 113.811),
    "Guangzhou": (23.392, 113.299), "Beijing": (40.080, 116.585),
    "Taipei": (25.069, 121.552), "Manila": (14.509, 121.020),
    "Kuala Lumpur": (2.746, 101.710), "Karachi": (24.893, 66.939),
    "Wuhan": (30.784, 114.208), "Qingdao": (36.362, 120.086),
    "Lucknow": (26.761, 80.889), "Jeddah": (21.679, 39.157),
    "Tel Aviv": (32.011, 34.887), "Toronto": (43.677, -79.625),
    "Sao Paulo": (-23.432, -46.470), "Wellington": (-41.327, 174.805),
    "Cape Town": (-33.965, 18.602), "Mexico City": (19.436, -99.072),
}

_MONTHS = {"January": "01", "February": "02", "March": "03", "April": "04", "May": "05",
           "June": "06", "July": "07", "August": "08", "September": "09",
           "October": "10", "November": "11", "December": "12"}


def to_iso(date, year="2026"):
    """'June 21' -> '2026-06-21'; None if unparseable."""
    try:
        mon, day = date.split()
        return f"{year}-{_MONTHS[mon]}-{int(day):02d}"
    except (ValueError, KeyError):
        return None


def _bucket_c(b):
    """Normalize a parsed bucket to °C so all cities aggregate in one unit."""
    if b["unit"] == "C":
        return b
    def f(x):
        return (x - 32) * 5.0 / 9.0
    return {"lo": f(b["lo"]), "hi": f(b["hi"]), "mid": f(b["mid"]), "unit": "C", "open": b["open"]}


def _daily_agg(values, kind):
    """Daily max (highest markets) or min (lowest markets) over an hourly series."""
    nums = [v for v in (values or []) if isinstance(v, (int, float))]
    if not nums:
        return None
    return max(nums) if kind == "highest" else min(nums)


def _agg_by_date(times, values, iso, kind):
    """Daily max/min over only the hours whose local timestamp falls on `iso`."""
    sel = [v for t, v in zip(times or [], values or [], strict=False)
           if isinstance(t, str) and t.startswith(iso)]
    return _daily_agg(sel, kind)


def forecast_error(daily_agg, bucket):
    """Signed error (forecast − actual) + whether the forecast lands in the YES bucket.
    'actual' = bucket midpoint. None if no forecast or the bucket is open-ended (its
    midpoint isn't a real center, so it can't be scored cleanly)."""
    if daily_agg is None or bucket is None or bucket.get("open"):
        return None
    err = daily_agg - bucket["mid"]
    return {"err": err, "abs": abs(err), "hit": bucket["lo"] <= daily_agg <= bucket["hi"]}


def summarize(errs):
    """errs: list of forecast_error dicts (None dropped). MAE, signed bias, hit-rate."""
    e = [x for x in errs if x is not None]
    n = len(e)
    if n == 0:
        return {"n": 0, "mae": None, "bias": None, "hit": None}
    return {"n": n, "mae": sum(x["abs"] for x in e) / n,
            "bias": sum(x["err"] for x in e) / n, "hit": sum(1 for x in e if x["hit"]) / n}


async def _get(client, url, params, tries=4):
    """GET with 429/error backoff. Returns parsed JSON or None."""
    for k in range(tries):
        try:
            r = await client.get(url, params=params)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(2 * (k + 1))
            continue
        if r.status_code == 429:
            await asyncio.sleep(min(float(r.headers.get("retry-after", 0) or 5 * (k + 1)), 30))
            continue
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return None
    return None


def _nth(data, j):
    """One location's object from a multi-location response (list) or single dict."""
    if isinstance(data, list):
        return data[j] if j < len(data) else None
    return data if j == 0 else None


async def run(*, days, cap, conc, chunk):
    import httpx

    clean, _none, _multi = await reconstruct(days=days, cap=cap, conc=conc)
    targets, skipped = [], set()
    for rec in clean:
        city, date, kind = rec["key"]
        if city not in STATION_COORDS:
            skipped.add(city)
            continue
        iso = to_iso(date)
        if iso:
            targets.append((city, iso, kind, _bucket_c(rec["yes"][0]["parsed"])))
    if not targets:
        print("no gradeable targets (no coords / dates)")
        return

    isos = sorted({t[1] for t in targets})
    start, end = isos[0], isos[-1]
    cities = sorted({t[0] for t in targets})
    stations = [(c, *STATION_COORDS[c]) for c in cities]
    print(f"grading {len(targets)} city-days · {len(stations)} stations · {start}..{end}  "
          f"({len(skipped)} cities unmapped: {', '.join(sorted(skipped))[:90]})")

    pr, ar = {}, {}   # city -> hourly dict ; city -> {iso: (max,min)}
    async with httpx.AsyncClient(timeout=90) as client:
        for i in range(0, len(stations), chunk):
            grp = stations[i:i + chunk]
            lats = ",".join(f"{s[1]}" for s in grp)
            lons = ",".join(f"{s[2]}" for s in grp)
            common = {"latitude": lats, "longitude": lons, "start_date": start,
                      "end_date": end, "timezone": "auto", "temperature_unit": "celsius"}
            prd = await _get(client, _OM, {**common, "hourly": _HOURLY, "models": _MODELS})
            ard = await _get(client, _ARCHIVE, {**common,
                                                "daily": ["temperature_2m_max", "temperature_2m_min"]})
            for j, (c, _lat, _lon) in enumerate(grp):
                pj, aj = _nth(prd, j), _nth(ard, j)
                pr[c] = pj.get("hourly", {}) if pj else {}
                da = aj.get("daily", {}) if aj else {}
                ar[c] = {d: (mx, mn) for d, mx, mn in zip(
                    da.get("time", []), da.get("temperature_2m_max", []),
                    da.get("temperature_2m_min", []), strict=False)}

    glob = defaultdict(list)
    bycity = defaultdict(lambda: defaultdict(list))
    for city, iso, kind, bucket in targets:
        h = pr.get(city, {})
        times = h.get("time", [])
        for model in _MODELS:
            for lead, suf in _LEADS:
                dagg = _agg_by_date(times, h.get(f"temperature_2m{suf}_{model}"), iso, kind)
                err = forecast_error(dagg, bucket)
                glob[(model, lead)].append(err)
                bycity[city][(model, lead)].append(err)
        era5 = ar.get(city, {}).get(iso)
        actual = (era5[0] if kind == "highest" else era5[1]) if era5 else None
        e_err = forecast_error(actual, bucket)
        glob[("era5", "obs")].append(e_err)
        bycity[city][("era5", "obs")].append(e_err)

    fid = summarize(glob[("era5", "obs")])
    print("\n===== DATA FIDELITY — ERA5 reanalysis 'actual' vs the WU resolution (°C) =====")
    if fid["n"]:
        sign = "HOTTER" if (fid["bias"] or 0) < 0 else "COOLER"
        print(f"  n={fid['n']}   MAE {fid['mae']:.2f}   bias {fid['bias']:+.2f}   "
              f"in-bucket {fid['hit'] * 100:.0f}%")
        print(f"  → WU settles {abs(fid['bias'] or 0):.2f}° {sign} than independent reanalysis "
              "on average.")
        print("    small MAE + low bias  → WU faithfully tracks reality, a good forecast can hit "
              "the bucket (bettable).")
        print("    big / biased          → WU is idiosyncratic: caps precision, OR the systematic "
              "bias IS the edge.")
    else:
        print("  (no ERA5 — dates likely within ERA5's ~5-day lag; widen --days for older days)")

    print("\n===== MARGIN OF FAILURE — forecast vs settled high (°C) =====")
    print(f"{'model':>14} {'lead':>5} {'n':>5} {'MAE':>6} {'bias':>7} {'hit%':>6}")
    best = {}
    for model in _MODELS:
        for lead, _ in _LEADS:
            s = summarize(glob[(model, lead)])
            if not s["n"]:
                print(f"{model:>14} {lead:>5} {0:>5}   (no data)")
                continue
            print(f"{model:>14} {lead:>5} {s['n']:>5} {s['mae']:>6.2f} "
                  f"{s['bias']:>+7.2f} {s['hit'] * 100:>5.0f}%")
            cur = best.get(lead)
            if cur is None or s["mae"] < cur[1]:
                best[lead] = (model, s["mae"])

    if best:
        print("\nbest model by lead (lowest MAE):  " +
              "   ".join(f"{lead}: {m} ({mae:.2f})" for lead, (m, mae) in best.items()))
        bm = best.get("24h", best.get("now"))[0]
        rows = [(city, s["n"], s["mae"], s["bias"]) for city, d in bycity.items()
                if (s := summarize(d[(bm, "24h")]))["n"]]
        print(f"\nWORST CITIES ({bm} @ 24h, by MAE — divergence or coord issues):")
        for city, n, mae, bias in sorted(rows, key=lambda x: -x[2])[:15]:
            print(f"  {city:<16} n={n:>3} MAE {mae:>5.2f}  bias {bias:>+5.2f}")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Step 2b-2: grade forecast margin of failure vs settled highs")
    ap.add_argument("--days", type=int, default=14, help="ground-truth lookback window")
    ap.add_argument("--cap", type=int, default=0, help="cap markets resolved (0 = all)")
    ap.add_argument("--conc", type=int, default=20, help="gamma resolution concurrency")
    ap.add_argument("--chunk", type=int, default=15, help="stations per multi-location Open-Meteo call")
    args = ap.parse_args()
    asyncio.run(run(days=args.days, cap=args.cap, conc=args.conc, chunk=args.chunk))


if __name__ == "__main__":
    main()
