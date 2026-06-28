"""Tests for the pure scoring core of scripts/weather_grade.py. The Open-Meteo + gamma
I/O is integration-level and runs on the VPS."""

from __future__ import annotations

from scripts.weather_grade import (
    _agg_by_date,
    _bucket_c,
    _daily_agg,
    forecast_error,
    summarize,
    to_iso,
)
from scripts.weather_truth import parse_bucket


def test_to_iso():
    assert to_iso("June 21") == "2026-06-21"
    assert to_iso("December 5") == "2026-12-05"
    assert to_iso("garbage") is None


def test_bucket_c_converts_fahrenheit():
    b = _bucket_c(parse_bucket("between 92-93°F"))   # 92°F=33.33, 93°F=33.89
    assert b["unit"] == "C"
    assert abs(b["lo"] - 33.333) < 0.01 and abs(b["hi"] - 33.889) < 0.01


def test_agg_by_date_slices_local_day_only():
    times = ["2026-06-21T00:00", "2026-06-21T14:00", "2026-06-22T03:00"]
    vals = [18.0, 29.0, 40.0]
    assert _agg_by_date(times, vals, "2026-06-21", "highest") == 29.0   # ignores the 22nd
    assert _agg_by_date(times, vals, "2026-06-22", "highest") == 40.0
    assert _agg_by_date(times, vals, "2026-06-23", "highest") is None


def test_daily_agg_max_min_empty():
    assert _daily_agg([20, 28, 24, 18], "highest") == 28
    assert _daily_agg([20, 28, 24, 18], "lowest") == 18
    assert _daily_agg([], "highest") is None
    assert _daily_agg([None, 22.5, None], "highest") == 22.5


def test_forecast_error_in_and_out_of_bucket():
    b = parse_bucket("28°C")                       # lo 27.5, hi 28.5, mid 28
    e_in = forecast_error(28.3, b)
    assert abs(e_in["err"] - 0.3) < 1e-9 and e_in["hit"] is True
    e_out = forecast_error(30.0, b)
    assert abs(e_out["err"] - 2.0) < 1e-9 and e_out["hit"] is False


def test_forecast_error_skips_open_and_missing():
    assert forecast_error(25.0, parse_bucket("30°C or below")) is None   # open-ended
    assert forecast_error(None, parse_bucket("28°C")) is None


def test_summarize_mae_bias_hit():
    b = parse_bucket("28°C")
    errs = [forecast_error(28.3, b), forecast_error(30.0, b), forecast_error(27.0, b)]
    s = summarize(errs)
    assert s["n"] == 3
    # errors: +0.3, +2.0, -1.0  -> MAE = 3.3/3 = 1.1, bias = 1.3/3 ≈ 0.433
    assert abs(s["mae"] - 1.1) < 1e-9
    assert abs(s["bias"] - (1.3 / 3)) < 1e-9
    assert abs(s["hit"] - (1 / 3)) < 1e-9          # only the +0.3 lands in-bucket


def test_summarize_empty():
    assert summarize([None, None])["n"] == 0
