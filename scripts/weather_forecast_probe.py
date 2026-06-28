"""weather_forecast_probe.py — STEP 2b-2 PROBE.

Confirm Open-Meteo's Previous Runs API gives us honest lead-time forecasts before
building the margin-of-failure grader. The Previous Runs API has NO daily aggregates,
so we pull the HOURLY lead-time series — temperature_2m_previous_day1 (forecast issued
~24h before valid time) and _previous_day2 (~48h before), per model — and compute the
daily MAX ourselves over the target day's hours. That daily max is the lead-time
forecast of the day's high, which we compare to the actual high pinned from gamma.

Resolution stations (from the gamma descriptions in step 1), all °C cities, June 21.
Run on the VPS:
    docker compose exec -T executor python -m scripts.weather_forecast_probe
"""

from __future__ import annotations

import argparse
import asyncio
import logging

_OM = "https://previous-runs-api.open-meteo.com/v1/forecast"
_HOURLY = ["temperature_2m", "temperature_2m_previous_day1", "temperature_2m_previous_day2"]
_MODELS = ["ecmwf_ifs04", "gfs_seamless", "icon_seamless"]

# (city, station, lat, lon, date, actual_high_°C from gamma ground truth)
_PROBE = [
    ("London", "London City EGLC", 51.505, 0.055, "2026-06-21", 28),
    ("Paris", "Le Bourget LFPB", 48.969, 2.441, "2026-06-21", 37),
    ("Madrid", "Barajas LEMD", 40.472, -3.561, "2026-06-21", 39),
    ("Munich", "Munich EDDM", 48.353, 11.786, "2026-06-21", 34),
]


def _daily_max(values):
    """Daily max over an hourly series, ignoring nulls; None if all null."""
    nums = [v for v in (values or []) if isinstance(v, (int, float))]
    return max(nums) if nums else None


async def run():
    import httpx
    async with httpx.AsyncClient(timeout=30) as c:
        for i, (city, stn, lat, lon, date, actual) in enumerate(_PROBE):
            params = {
                "latitude": lat, "longitude": lon,
                "start_date": date, "end_date": date,
                "hourly": _HOURLY, "models": _MODELS, "timezone": "auto",
            }
            try:
                r = await c.get(_OM, params=params)
                if r.status_code != 200:
                    print(f"{city}: HTTP {r.status_code} — {r.text[:220]}")
                    continue
                d = r.json()
            except Exception as e:  # noqa: BLE001
                print(f"{city}: request failed: {type(e).__name__}: {e}")
                continue
            hourly = d.get("hourly", {})
            if i == 0:
                print(f"RAW hourly keys ({city}): {list(hourly.keys())}")
            print(f"\n{city} {date} @ {stn} — ACTUAL high {actual}°C  "
                  f"(daily max of each lead-time hourly series):")
            for k in sorted(hourly):
                if k == "time":
                    continue
                dmax = _daily_max(hourly[k])
                if dmax is None:
                    print(f"   {k:<48} (no data)")
                else:
                    print(f"   {k:<48} {dmax:6.1f}  (Δ vs actual {dmax - actual:+.1f}°C)")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    argparse.ArgumentParser(description="Step 2b-2 probe: Open-Meteo Previous Runs lead-time forecasts").parse_args()
    asyncio.run(run())


if __name__ == "__main__":
    main()
