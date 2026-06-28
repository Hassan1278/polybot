"""weather_grade.py — STEP 2b-2: grade the forecasts' MARGIN OF FAILURE against the
gamma ground truth, per model and lead time. No model-building — we're scoring existing
professional forecasts against the value that actually settled each market.

For every clean city-day high (from weather_truth.reconstruct), pull Open-Meteo's
lead-time forecasts (Previous Runs API: 24h & 48h ahead, plus the latest run) at the
market's RESOLUTION-STATION coordinates, in the bucket's native unit, and compute:
  • per model × lead: MAE, signed bias, and hit-rate (forecast lands in the YES bucket);
  • per city: which locations the forecasts miss most;
  • the systematic-bias check — does the settled high run consistently hotter/cooler
    than the forecast (e.g. the Wunderground airport-max-runs-hot effect)?

DATA FIDELITY: also pulls ERA5 reanalysis (an independent "what actually happened") and
compares it to the WU resolution — isolating whether the WU number itself is a faithful,
bettable representation of reality or an idiosyncratic source that caps precision.

STATION_COORDS are the resolution airports from the step-1 gamma descriptions — verify/
extend as needed; unmapped cities are skipped and reported. Run on the VPS:
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


def _daily_agg(values, kind):
    """Daily max (highest markets) or min (lowest markets) over an hourly series."""
    nums = [v for v in (values or []) if isinstance(v, (int, float))]
    if not nums:
        return None
    return max(nums) if kind == "highest" else min(nums)


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


async def _forecast(client, lat, lon, date, unit, kind):
    """{(model, lead): daily_agg} for one station/day, or {} on failure."""
    try:
        r = await client.get(_OM, params={
            "latitude": lat, "longitude": lon, "start_date": date, "end_date": date,
            "hourly": _HOURLY, "models": _MODELS, "timezone": "auto",
            "temperature_unit": unit})
        if r.status_code != 200:
            return {}
        h = (r.json() or {}).get("hourly", {})
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for model in _MODELS:
        for lead, suf in _LEADS:
            out[(model, lead)] = _daily_agg(h.get(f"temperature_2m{suf}_{model}"), kind)
    return out


async def _era5_actual(client, lat, lon, iso, unit, kind):
    """ERA5 reanalysis daily max/min — an INDEPENDENT 'what actually happened' at the
    station, to compare against the Wunderground resolution. None if unavailable (ERA5
    lags ~5 days, so the most recent days won't be in yet)."""
    var = "temperature_2m_max" if kind == "highest" else "temperature_2m_min"
    try:
        r = await client.get(_ARCHIVE, params={
            "latitude": lat, "longitude": lon, "start_date": iso, "end_date": iso,
            "daily": var, "timezone": "auto", "temperature_unit": unit})
        if r.status_code != 200:
            return None
        d = (r.json() or {}).get("daily", {}).get(var)
    except Exception:  # noqa: BLE001
        return None
    return d[0] if isinstance(d, list) and d and isinstance(d[0], (int, float)) else None


async def run(*, days, cap, conc, fc_conc):
    import httpx

    clean, _none, _multi = await reconstruct(days=days, cap=cap, conc=conc)
    targets, skipped_city = [], set()
    for rec in clean:
        city, date, kind = rec["key"]
        if city not in STATION_COORDS:
            skipped_city.add(city)
            continue
        bucket = rec["yes"][0]["parsed"]
        targets.append((city, date, kind, bucket))

    def to_iso(date):
        # ground-truth dates look like "June 21"; map month name → 2026-06-DD
        months = {"January": "01", "February": "02", "March": "03", "April": "04",
                  "May": "05", "June": "06", "July": "07", "August": "08",
                  "September": "09", "October": "10", "November": "11", "December": "12"}
        try:
            mon, day = date.split()
            return f"2026-{months[mon]}-{int(day):02d}"
        except (ValueError, KeyError):
            return None

    print(f"grading {len(targets)} city-days with coords "
          f"({len(skipped_city)} cities unmapped: {', '.join(sorted(skipped_city))[:120]})")

    glob = defaultdict(list)
    bycity = defaultdict(lambda: defaultdict(list))
    sem = asyncio.Semaphore(fc_conc)

    async def grade(client, city, date, kind, bucket):
        iso = to_iso(date)
        if iso is None:
            return
        unit = "fahrenheit" if bucket["unit"] == "F" else "celsius"
        lat, lon = STATION_COORDS[city]
        async with sem:
            fc = await _forecast(client, lat, lon, iso, unit, kind)
            era5 = await _era5_actual(client, lat, lon, iso, unit, kind)
        e_err = forecast_error(era5, bucket)
        glob[("era5_actual", "obs")].append(e_err)
        bycity[city][("era5_actual", "obs")].append(e_err)
        for key, dagg in fc.items():
            err = forecast_error(dagg, bucket)
            glob[key].append(err)
            bycity[city][key].append(err)

    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(*[grade(client, *t) for t in targets])

    fid = summarize(glob[("era5_actual", "obs")])
    print("\n===== DATA FIDELITY — ERA5 reanalysis 'actual' vs the WU resolution =====")
    if fid["n"]:
        sign = "HOTTER" if (fid["bias"] or 0) < 0 else "COOLER"
        print(f"  n={fid['n']}   MAE {fid['mae']:.2f}°   bias {fid['bias']:+.2f}°   "
              f"in-bucket {fid['hit'] * 100:.0f}%")
        print(f"  → the WU settlement runs {abs(fid['bias'] or 0):.2f}° {sign} than independent "
              "reanalysis on average.")
        print("    small MAE + low bias = WU faithfully tracks reality → a good forecast can hit "
              "the bucket (bettable).")
        print("    big/biased = WU is idiosyncratic → either it caps how precisely anyone can bet, "
              "OR a systematic bias IS the edge.")
    else:
        print("  (no ERA5 data — likely the dates are within ERA5's ~5-day lag; widen --days)")

    print("\n===== MARGIN OF FAILURE — forecast vs settled high =====")
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
            best.setdefault(lead, (model, s["mae"]))
            if s["mae"] < best[lead][1]:
                best[lead] = (model, s["mae"])

    print("\nbest model by lead (lowest MAE):  " +
          "   ".join(f"{lead}: {m} ({mae:.2f})" for lead, (m, mae) in best.items()))
    bm = best.get("24h", best.get("now", (None, None)))[0]
    if bm:
        bs = summarize(glob[(bm, "24h")])
        sign = "HOTTER" if bs["bias"] and bs["bias"] < 0 else "COOLER"
        print(f"\nSYSTEMATIC BIAS ({bm} @ 24h): settled high runs {abs(bs['bias'] or 0):.2f}° "
              f"{sign} than forecast on average (bias {bs['bias']:+.2f}). "
              "Consistent sign = an exploitable offset; ~0 = forecasts are centered.")

        rows = []
        for city, d in bycity.items():
            s = summarize(d[(bm, "24h")])
            if s["n"]:
                rows.append((city, s["n"], s["mae"], s["bias"]))
        print(f"\nWORST CITIES ({bm} @ 24h, by MAE — divergence / possible coord issues):")
        for city, n, mae, bias in sorted(rows, key=lambda x: -x[2])[:15]:
            print(f"  {city:<16} n={n:>3} MAE {mae:>5.2f}  bias {bias:>+5.2f}")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Step 2b-2: grade forecast margin of failure vs settled highs")
    ap.add_argument("--days", type=int, default=14, help="ground-truth lookback window")
    ap.add_argument("--cap", type=int, default=0, help="cap markets resolved (0 = all)")
    ap.add_argument("--conc", type=int, default=20, help="gamma resolution concurrency")
    ap.add_argument("--fc-conc", type=int, default=8, help="Open-Meteo forecast concurrency")
    args = ap.parse_args()
    asyncio.run(run(days=args.days, cap=args.cap, conc=args.conc, fc_conc=args.fc_conc))


if __name__ == "__main__":
    main()
