"""weather_forecast_probe.py — STEP 2b-2 PROBE.

Before building the margin-of-failure grader, confirm Open-Meteo's archived-forecast API
shape on a few city-days whose ACTUAL high we already pinned from gamma. Critically:
does it give us the forecast as it stood ~24h/48h BEFORE the date (the `_previous_dayN`
lead-time variables), per model — not a hindsight analysis? We dump the raw `daily`
dict for the first city so we can see exactly what keys come back.

Resolution stations (from the gamma descriptions in step 1), all °C cities, June 21:
  London City (EGLC) · Paris-Le Bourget (LFPB) · Madrid-Barajas (LEMD) · Munich (EDDM)

Run on the VPS:
    docker compose exec -T executor python -m scripts.weather_forecast_probe
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

_OM = "https://historical-forecast-api.open-meteo.com/v1/forecast"
_MODELS = "ecmwf_ifs04,gfs_seamless,icon_seamless"

# (city, station, lat, lon, date, actual_high_°C from gamma ground truth)
_PROBE = [
    ("London", "London City EGLC", 51.505, 0.055, "2026-06-21", 28),
    ("Paris", "Le Bourget LFPB", 48.969, 2.441, "2026-06-21", 37),
    ("Madrid", "Barajas LEMD", 40.472, -3.561, "2026-06-21", 39),
    ("Munich", "Munich EDDM", 48.353, 11.786, "2026-06-21", 34),
]


async def run():
    import httpx
    async with httpx.AsyncClient(timeout=30) as c:
        for i, (city, stn, lat, lon, date, actual) in enumerate(_PROBE):
            params = {
                "latitude": lat, "longitude": lon,
                "start_date": date, "end_date": date,
                "daily": "temperature_2m_max,temperature_2m_max_previous_day1,"
                         "temperature_2m_max_previous_day2",
                "models": _MODELS, "timezone": "auto",
            }
            try:
                r = await c.get(_OM, params=params)
                if r.status_code != 200:
                    print(f"{city}: HTTP {r.status_code} — {r.text[:200]}")
                    continue
                d = r.json()
            except Exception as e:  # noqa: BLE001
                print(f"{city}: request failed: {type(e).__name__}: {e}")
                continue
            daily = d.get("daily", {})
            if i == 0:
                print(f"RAW daily keys ({city}): {list(daily.keys())}")
                print(json.dumps(daily, indent=2)[:1400])
            print(f"\n{city} {date} @ {stn} — ACTUAL high {actual}°C")
            for k, v in daily.items():
                if k == "time":
                    continue
                val = v[0] if isinstance(v, list) and v else v
                tag = ""
                if isinstance(val, (int, float)):
                    tag = f"  (Δ vs actual {val - actual:+.1f}°C)"
                print(f"   {k:<46} {val}{tag}")


def main():
    for _n in ("httpx", "httpcore"):
        logging.getLogger(_n).setLevel(logging.WARNING)
    argparse.ArgumentParser(description="Step 2b-2 probe: confirm Open-Meteo archived-forecast shape").parse_args()
    asyncio.run(run())


if __name__ == "__main__":
    main()
