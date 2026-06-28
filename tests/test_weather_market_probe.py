"""Test the pure price-sampling helper in scripts/weather_market_probe.py. The CLOB +
gamma I/O is integration-level and runs on the VPS."""

from __future__ import annotations

from scripts.weather_market_probe import _sample_at


def test_sample_at_picks_closest_point():
    hist = [{"t": 1000, "p": 0.30}, {"t": 2000, "p": 0.45}, {"t": 3000, "p": 0.60}]
    assert _sample_at(hist, 1900) == (0.45, 2000)      # closest to 1900 is t=2000
    assert _sample_at(hist, 2600) == (0.60, 3000)
    assert _sample_at(hist, 0) == (0.30, 1000)         # before all → earliest


def test_sample_at_empty():
    assert _sample_at([], 1000) == (None, None)
    assert _sample_at(None, 1000) == (None, None)
