"""Tests for the live equity-drawdown circuit breaker decision:
`services/executor/equity_guard.drawdown_breached`.

The I/O parts (reading venue equity, Redis baseline, tripping the kill switch)
are integration-level and fail-safe by construction; the breach decision is the
pure, testable core.
"""

from __future__ import annotations

from services.executor.equity_guard import _trading_day, drawdown_breached


def test_breaches_above_threshold():
    # 16% drop, 15% limit -> trip.
    assert drawdown_breached(100.0, 84.0, 0.15) is True


def test_no_breach_within_tolerance():
    # 10% drop, 15% limit -> fine.
    assert drawdown_breached(100.0, 90.0, 0.15) is False


def test_exact_threshold_breaches():
    # Exactly 15% counts as a breach (>=).
    assert drawdown_breached(100.0, 85.0, 0.15) is True


def test_gain_never_breaches():
    assert drawdown_breached(100.0, 130.0, 0.15) is False


def test_nonpositive_baseline_is_safe():
    # No baseline -> can't define a drawdown -> never trips (fail-safe).
    assert drawdown_breached(0.0, 0.0, 0.15) is False
    assert drawdown_breached(-5.0, -50.0, 0.15) is False


def test_total_wipe_breaches():
    assert drawdown_breached(200.0, 0.01, 0.15) is True


def test_trading_day_is_iso_date():
    # Default reset hour 0 -> a plain ISO date string (YYYY-MM-DD).
    d = _trading_day(0)
    assert len(d) == 10 and d.count("-") == 2
