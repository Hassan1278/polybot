"""Tests for the pure core of scripts/weather_truth.py — bucket parsing and the
ladder → actual-high reconstruction. The gamma I/O runs on the VPS."""

from __future__ import annotations

import math

from scripts.weather_truth import actual_high, parse_bucket


def test_parse_bucket_point_celsius():
    b = parse_bucket("28°C")
    assert b["unit"] == "C" and b["mid"] == 28.0
    assert b["lo"] == 27.5 and b["hi"] == 28.5 and b["open"] is False


def test_parse_bucket_fahrenheit_range():
    b = parse_bucket("between 92-93°F")
    assert b["unit"] == "F" and b["lo"] == 92.0 and b["hi"] == 93.0
    assert b["mid"] == 92.5 and b["open"] is False


def test_parse_bucket_open_ended():
    lo = parse_bucket("37°C or below")
    assert lo["unit"] == "C" and lo["hi"] == 37.0 and lo["open"] is True and lo["lo"] < 0
    hi = parse_bucket("40°C or above")
    assert hi["lo"] == 40.0 and hi["open"] is True and hi["hi"] > 90
    # the real Polymarket label is "or higher", not "or above"
    hr = parse_bucket("38°C or higher")
    assert hr["lo"] == 38.0 and hr["open"] is True


def test_parse_bucket_no_number():
    assert parse_bucket("not a temperature") is None


def test_actual_high_clean_single_yes():
    legs = [
        {"bucket": "27°C", "yes": False},
        {"bucket": "28°C", "yes": True},
        {"bucket": "29°C", "yes": False},
    ]
    status, yes = actual_high(legs)
    assert status == "clean" and len(yes) == 1 and yes[0]["bucket"] == "28°C"


def test_actual_high_flags_none_and_multi():
    assert actual_high([{"bucket": "27°C", "yes": False}])[0] == "none"
    assert actual_high([{"bucket": "27°C", "yes": True},
                        {"bucket": "28°C", "yes": True}])[0] == "multi"


def test_buckets_are_disjoint_and_ordered():
    # a real ladder's point buckets shouldn't overlap (sanity on the bounds convention)
    a, b = parse_bucket("31°C"), parse_bucket("32°C")
    assert a["hi"] <= b["lo"] + 1e-9 and not math.isclose(a["mid"], b["mid"])
