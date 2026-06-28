"""Test the pure aggregation in scripts/weather_verify.py (used identically for the
Data API and the DB so the comparison is apples-to-apples). The HTTP/DB I/O is run
on the VPS."""

from __future__ import annotations

from scripts.weather_verify import _notional, tally


def test_tally_buckets_side_and_outcome():
    items = [
        ("m1", "BUY", "NO", 6.0),
        ("m1", "SELL", "NO", 2.0),
        ("m2", "BUY", "YES", 3.0),
    ]
    t = tally(items)
    assert t["n"] == 3
    assert t["markets"] == {"m1", "m2"}
    assert abs(t["buy"] - 9.0) < 1e-9
    assert abs(t["sell"] - 2.0) < 1e-9
    assert t["no_n"] == 2 and t["yes_n"] == 1


def test_notional_prefers_usdcsize_else_size_times_price():
    assert abs(_notional({"usdcSize": "7.5"}) - 7.5) < 1e-9
    assert abs(_notional({"size": "10", "price": "0.6"}) - 6.0) < 1e-9
    assert _notional({}) == 0.0
