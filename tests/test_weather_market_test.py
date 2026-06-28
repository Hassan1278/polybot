"""Tests for the pure decision core of scripts/weather_market_test.py. The gamma / CLOB /
Open-Meteo I/O is integration-level and runs on the VPS."""

from __future__ import annotations

from scripts.weather_grade import _bucket_c
from scripts.weather_market_test import _verdict, match_bucket, summarize_edge
from scripts.weather_truth import parse_bucket


def _b(*labels):
    return [(lbl, _bucket_c(parse_bucket(lbl))) for lbl in labels]


def test_match_bucket_containment_and_nearest():
    bks = _b("27°C", "28°C", "29°C")
    assert match_bucket(28.2, bks) == "28°C"
    assert match_bucket(26.9, bks) == "27°C"          # outside all → nearest by mid
    assert match_bucket(None, bks) is None


def test_match_bucket_prefers_narrow_over_open():
    bks = _b("30°C or below", "28°C", "29°C")
    assert match_bucket(28.0, bks) == "28°C"          # the 1° bucket, not the wide open one


def test_summarize_edge_basic():
    s = summarize_edge([(1.0, 0.3), (0.0, 0.4), (1.0, 0.5)])
    assert s["n"] == 3
    assert abs(s["edge"] - (0.7 - 0.4 + 0.5) / 3) < 1e-9
    assert abs(s["hit"] - 2 / 3) < 1e-9
    assert abs(s["price"] - 0.4) < 1e-9


def test_summarize_edge_empty():
    assert summarize_edge([])["n"] == 0


def test_verdict_thresholds():
    assert _verdict({"n": 10, "edge": 0.10, "se": 0.02}) == "EDGE (forecast beats market)"
    assert _verdict({"n": 10, "edge": -0.10, "se": 0.02}).startswith("NEGATIVE")
    assert "efficient" in _verdict({"n": 10, "edge": 0.01, "se": 0.02})
    assert _verdict({"n": 0}) == "n/a"
