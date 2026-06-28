"""Tests for the pure probability/calibration core of scripts/weather_market_full.py.
The CLOB/gamma/Open-Meteo I/O is integration-level and runs on the VPS."""

from __future__ import annotations

from scripts.weather_market_full import brier, gauss_bucket_prob
from scripts.weather_truth import parse_bucket


def test_gauss_point_bucket_atm():
    p = gauss_bucket_prob(28.0, 1.4, parse_bucket("28°C"))   # 1° band centered on the mean
    assert 0.20 < p < 0.35                                    # ≈ 0.278


def test_gauss_far_bucket_near_zero():
    assert gauss_bucket_prob(28.0, 1.4, parse_bucket("35°C")) < 0.001


def test_gauss_open_below_and_higher():
    pb = gauss_bucket_prob(28.0, 1.4, parse_bucket("30°C or below"))   # CDF(2/1.4)
    assert 0.88 < pb < 0.95
    ph = gauss_bucket_prob(28.0, 1.4, parse_bucket("26°C or higher"))  # 1−CDF(−2/1.4)
    assert 0.88 < ph < 0.95


def test_gauss_guards():
    assert gauss_bucket_prob(None, 1.4, parse_bucket("28°C")) is None
    assert gauss_bucket_prob(28.0, 0.0, parse_bucket("28°C")) is None


def test_brier():
    rows = [{"p_fc": 0.8, "won": 1.0}, {"p_fc": 0.2, "won": 0.0}, {"p_fc": 0.5, "won": 1.0}]
    # (0.2² + 0.2² + 0.5²)/3 = 0.11
    assert abs(brier(rows, "p_fc") - 0.11) < 1e-9
    assert brier([{"won": 1.0}], "p_fc") is None
