"""Tests for the live equity-drawdown circuit breaker decision:
`services/executor/equity_guard.drawdown_breached`.

The I/O parts (reading venue equity, Redis baseline, tripping the kill switch)
are integration-level and fail-safe by construction; the breach decision is the
pure, testable core.
"""

from __future__ import annotations

from services.executor.equity_guard import (
    _trading_day,
    breaker_action,
    drawdown_breached,
    drawdown_recovered,
)


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


# ── drawdown_recovered (auto-resume threshold) ───────────────────────────────

def test_recovered_at_resume_threshold():
    # 10% below the open, resume line at 10% -> recovered (<=).
    assert drawdown_recovered(100.0, 90.0, 0.10) is True


def test_not_recovered_while_still_deep():
    # 16% below the open, resume line 10% -> not yet.
    assert drawdown_recovered(100.0, 84.0, 0.10) is False


def test_recovered_when_above_open():
    assert drawdown_recovered(100.0, 130.0, 0.10) is True


def test_recovered_disabled_when_unset_or_bad_baseline():
    assert drawdown_recovered(100.0, 90.0, None) is False
    assert drawdown_recovered(100.0, 90.0, 0.0) is False
    assert drawdown_recovered(0.0, 0.0, 0.10) is False


# ── breaker_action (per-tick resume/trip/noop decision) ──────────────────────

def test_action_trips_when_breached_and_unhalted():
    assert breaker_action(None, breached=True, recovered=False) == "trip"


def test_action_noop_when_calm_and_unhalted():
    assert breaker_action(None, breached=False, recovered=False) == "noop"


def test_action_resumes_our_kill_once_recovered():
    k = "equity_drawdown:15.2%>=15%:baseline=291.00:now=247.00"
    assert breaker_action(k, breached=False, recovered=True) == "resume"


def test_action_holds_our_kill_until_recovered():
    # Between the resume and trip lines -> stay halted, don't re-trip.
    k = "equity_drawdown:12.0%>=15%:baseline=100.00:now=88.00"
    assert breaker_action(k, breached=False, recovered=False) == "noop"


def test_action_leaves_foreign_kill_untouched():
    # A manual / other halt is never auto-cleared, even if recovered,
    assert breaker_action("manual:operator", breached=False, recovered=True) == "noop"
    # and we never trip on top of an existing foreign halt.
    assert breaker_action("live_mode_no_creds", breached=True, recovered=False) == "noop"
