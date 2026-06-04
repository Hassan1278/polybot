"""Tests for `polybot.stats.position_size_from_score`.

Math under test (with defaults `anchor=0.5`, `steepness=2.0`):
    multiplier = clamp(1 + steepness * (score - anchor), 0.25, 3.0)
    size       = clamp(base * multiplier, base * 0.25, max_usdc)
"""

from __future__ import annotations

import pytest

from polybot.stats import position_size_from_score


# ── happy-path ────────────────────────────────────────────────────────────────

def test_position_size_at_anchor_returns_base():
    # score == anchor → multiplier == 1.0 → size == base
    size = position_size_from_score(0.5, base_usdc=10.0, max_usdc=100.0)
    assert size == pytest.approx(10.0)


def test_position_size_high_score_scales_up():
    # score=1.0, steepness=2.0 → raw multiplier = 1 + 2*(1-0.5) = 2.0 → 2x base
    size = position_size_from_score(1.0, base_usdc=10.0, max_usdc=100.0)
    assert size == pytest.approx(20.0)


# ── edge cases ────────────────────────────────────────────────────────────────

def test_position_size_low_score_is_floored():
    # score=0.0 → raw multiplier = 1 + 2*(0-0.5) = 0.0, clamped up to 0.25
    # → size == base * 0.25 == 2.5
    size = position_size_from_score(0.0, base_usdc=10.0, max_usdc=100.0)
    assert size == pytest.approx(2.5)


def test_position_size_respects_max_cap():
    # steepness=10 at score=1.0 → raw multiplier = 1 + 10*0.5 = 6.0
    # → clamped to 3.0 → size = 30; but max_usdc=20 should cap that further.
    size = position_size_from_score(
        1.0, base_usdc=10.0, max_usdc=20.0, steepness=10.0,
    )
    assert size == pytest.approx(20.0)


def test_position_size_zero_base_returns_zero():
    # Guard: base <= 0 or max <= 0 → 0.0 (no implicit negative sizing).
    assert position_size_from_score(0.9, base_usdc=0.0, max_usdc=100.0) == 0.0
    assert position_size_from_score(0.9, base_usdc=10.0, max_usdc=0.0) == 0.0


def test_position_size_score_above_one_is_tolerated():
    # Out-of-range score is clamped via the multiplier clamp — no exception.
    size = position_size_from_score(
        1.5, base_usdc=10.0, max_usdc=100.0, steepness=5.0,
    )
    # raw multiplier = 1 + 5*(1.5-0.5) = 6.0 → clamped to 3.0 → 30 USDC
    assert size == pytest.approx(30.0)


def test_position_size_negative_score_clamps_to_floor():
    # Out-of-range negative score should not produce a negative size.
    size = position_size_from_score(-1.0, base_usdc=8.0, max_usdc=100.0)
    assert size == pytest.approx(2.0)  # 8 * 0.25 floor
    assert size > 0
