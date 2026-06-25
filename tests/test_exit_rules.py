"""Tests for the stop-loss-only exit decision in services/executor/exit_rules.py.

Operator model: no early exits (no take-profit / thesis / near-expiry / sentiment).
The only exit is a stop-loss ladder:
  base 0.20; entry below 0.20 -> 0.05; once mark ever exceeds 0.75 -> 0.43.
The I/O parts (CLOB mark, Redis high-water flag, close path) are integration-level;
the level + trigger are the pure, testable core.
"""

from __future__ import annotations

from services.executor.exit_rules import stop_exit_reason, stop_loss_level

CFG = {
    "stop_loss_level": 0.20,
    "low_entry_threshold": 0.20,
    "low_entry_stop": 0.05,
    "profit_lock_trigger": 0.75,
    "profit_lock_stop": 0.43,
}


# ── stop_loss_level (which stop applies) ─────────────────────────────────────

def test_level_base_stop_for_normal_entry():
    assert stop_loss_level(avg_entry=0.50, hit_high_water=False, cfg=CFG) == 0.20


def test_level_loose_stop_for_low_entry():
    # Entered below 0.20 -> looser 0.05 stop (longshot needs room).
    assert stop_loss_level(avg_entry=0.15, hit_high_water=False, cfg=CFG) == 0.05


def test_level_profit_lock_overrides_everything():
    # Once it hit 0.75, the stop is 0.43 regardless of entry — even a low entry.
    assert stop_loss_level(avg_entry=0.50, hit_high_water=True, cfg=CFG) == 0.43
    assert stop_loss_level(avg_entry=0.10, hit_high_water=True, cfg=CFG) == 0.43


# ── stop_exit_reason (the only trigger; no take-profit) ──────────────────────

def test_no_early_exit_a_winner_is_held():
    # Up huge but never hit the lock trigger -> HELD (no take-profit).
    assert stop_exit_reason(avg_entry=0.50, mark=0.99, hit_high_water=False, cfg=CFG) is None


def test_base_stop_fires_at_or_below_020():
    assert stop_exit_reason(avg_entry=0.50, mark=0.21, hit_high_water=False, cfg=CFG) is None
    assert stop_exit_reason(avg_entry=0.50, mark=0.20, hit_high_water=False, cfg=CFG) == "stop_loss"
    assert stop_exit_reason(avg_entry=0.50, mark=0.10, hit_high_water=False, cfg=CFG) == "stop_loss"


def test_low_entry_uses_005_stop():
    # Entered at 0.15: holds at 0.10 (above 0.05), stops at 0.05.
    assert stop_exit_reason(avg_entry=0.15, mark=0.10, hit_high_water=False, cfg=CFG) is None
    assert stop_exit_reason(avg_entry=0.15, mark=0.05, hit_high_water=False, cfg=CFG) == "stop_loss"


def test_profit_lock_stop_at_043_after_hitting_075():
    # Ran past 0.75, now ratcheted: holds above 0.43, exits at/below it.
    assert stop_exit_reason(avg_entry=0.50, mark=0.44, hit_high_water=True, cfg=CFG) is None
    assert stop_exit_reason(avg_entry=0.50, mark=0.43, hit_high_water=True, cfg=CFG) == "profit_lock"
    assert stop_exit_reason(avg_entry=0.30, mark=0.40, hit_high_water=True, cfg=CFG) == "profit_lock"


def test_profit_lock_overrides_low_entry_stop():
    # A cheap entry that ran to 0.75+ then fell to 0.40: 0.43 stop applies (not 0.05).
    assert stop_exit_reason(avg_entry=0.10, mark=0.40, hit_high_water=True, cfg=CFG) == "profit_lock"


def test_guards_on_zero_or_negative_prices():
    assert stop_exit_reason(avg_entry=0.0, mark=0.5, hit_high_water=False, cfg=CFG) is None
    assert stop_exit_reason(avg_entry=0.5, mark=0.0, hit_high_water=False, cfg=CFG) is None
