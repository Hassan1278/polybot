"""Tests for the new entry gates in services/executor/risk.py:
category_blocked (disable a whole category on a mode) and entry_below_floor
(refuse sub-floor-priced entries). Pure decisions; the rest of preflight is
integration-level (DB + Redis)."""

from __future__ import annotations

from services.executor.risk import category_blocked, entry_below_floor

# ── category_blocked ─────────────────────────────────────────────────────────

def test_category_blocked_when_listed():
    cfg = {"disabled_categories": ["crypto"]}
    assert category_blocked("crypto", cfg) is True
    assert category_blocked("Crypto", cfg) is True        # case-insensitive


def test_category_not_blocked_when_absent():
    cfg = {"disabled_categories": ["crypto"]}
    assert category_blocked("weather", cfg) is False
    assert category_blocked("politics", cfg) is False


def test_category_block_disabled_by_default():
    assert category_blocked("crypto", {}) is False        # no list -> never blocks
    assert category_blocked("crypto", {"disabled_categories": []}) is False
    assert category_blocked(None, {"disabled_categories": ["crypto"]}) is False


# ── entry_below_floor ────────────────────────────────────────────────────────

def test_entry_below_floor_blocks_cheap_entries():
    cfg = {"min_entry_price": 0.07}
    assert entry_below_floor(0.05, cfg) == 0.07           # below floor -> the floor
    assert entry_below_floor(0.069, cfg) == 0.07


def test_entry_at_or_above_floor_passes():
    cfg = {"min_entry_price": 0.07}
    assert entry_below_floor(0.07, cfg) is None           # exactly the floor is OK
    assert entry_below_floor(0.50, cfg) is None


def test_entry_floor_ignores_nonpositive_and_disabled():
    # price 0 (unknown) never blocks; no/zero floor disables the gate.
    assert entry_below_floor(0.0, {"min_entry_price": 0.07}) is None
    assert entry_below_floor(0.05, {}) is None
    assert entry_below_floor(0.05, {"min_entry_price": None}) is None
