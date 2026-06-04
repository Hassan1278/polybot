from __future__ import annotations

import pytest

from services.executor.paper import _walk


def test_walk_fills_within_first_level():
    levels = [{"price": "0.30", "size": "100"}, {"price": "0.32", "size": "100"}]
    shares, notional, avg = _walk(levels, target_usdc=15.0)
    assert pytest.approx(notional) == 15.0
    assert pytest.approx(shares) == 50.0
    assert pytest.approx(avg) == 0.30


def test_walk_eats_multiple_levels():
    levels = [{"price": "0.30", "size": "100"}, {"price": "0.40", "size": "100"}]
    shares, notional, avg = _walk(levels, target_usdc=50.0)
    # first level supplies 30 USDC, need 20 more from level 2 (50 sh @ 0.4 = 20)
    assert pytest.approx(notional, 0.001) == 50.0
    assert pytest.approx(shares, 0.001) == 150.0
    assert 0.30 < avg < 0.40


def test_walk_insufficient_returns_none():
    levels = [{"price": "0.30", "size": "10"}]
    assert _walk(levels, target_usdc=100.0) is None
