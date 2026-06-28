"""Tests for the pure calibration core (scripts/calibration_backtest.py).

The math — outcome decoding, midlife price sampling, per-bucket calibration
(yes_rate vs price), and the cheap/dear bias summary — is pure and exact; the
price-history I/O is integration-level and run on the VPS.
"""

from __future__ import annotations

from scripts.calibration_backtest import (
    bias_summary,
    calibration_table,
    outcome_from_history,
    resolved_yes,
    sample_at_fraction,
)

# ── outcome_from_history (terminal price -> winner) ──────────────────────────

def test_outcome_from_terminal_price():
    assert outcome_from_history([(0, 0.4), (9, 0.98)]) == 1      # settled to ~1 -> YES won
    assert outcome_from_history([(0, 0.6), (9, 0.01)]) == 0      # settled to ~0 -> NO won
    assert outcome_from_history([(0, 0.5), (9, 0.55)]) is None   # still ambiguous -> unsettled
    assert outcome_from_history([]) is None

# ── resolved_yes (did the YES outcome win?) ──────────────────────────────────

def test_resolved_yes_binary():
    assert resolved_yes("Yes", ["Yes", "No"]) == 1
    assert resolved_yes("No", ["Yes", "No"]) == 0
    assert resolved_yes("yes", ["Yes", "No"]) == 1          # case-insensitive


def test_resolved_yes_multi_outcome():
    outs = ["TYLOO", "Lynn Vision"]
    assert resolved_yes("TYLOO", outs) == 1                 # outcomes[0] won -> YES
    assert resolved_yes("Lynn Vision", outs) == 0           # a different listed outcome won


def test_resolved_yes_undeterminable():
    assert resolved_yes(None, ["Yes", "No"]) is None
    assert resolved_yes("Yes", None) is None
    assert resolved_yes("Maybe", ["Yes", "No"]) is None     # winner not in the list


# ── sample_at_fraction (midlife price of genuine uncertainty) ────────────────

def test_sample_midlife_ignores_resolved_endpoints():
    # valid (0<p<1) points are at ts 10/50/90; midpoint by time -> ts 50, p 0.5.
    hist = [(0, 0.0), (10, 0.2), (50, 0.5), (90, 0.8), (100, 1.0)]
    assert sample_at_fraction(hist, 0.5)[1] == 0.5
    assert sample_at_fraction(hist, 0.0)[1] == 0.2          # earliest uncertain
    assert sample_at_fraction(hist, 1.0)[1] == 0.8          # latest uncertain


def test_sample_too_short_or_certain():
    assert sample_at_fraction([], 0.5) is None
    assert sample_at_fraction([(0, 0.5)], 0.5) is None              # <2 valid points
    assert sample_at_fraction([(0, 0.0), (1, 1.0)], 0.5) is None    # no uncertain points


# ── calibration_table (the curve) ────────────────────────────────────────────

def test_calibration_perfectly_calibrated_has_zero_edge():
    samples = [(0.72, 1)] * 72 + [(0.72, 0)] * 28           # 0.72 priced, 72% realized
    row = next(r for r in calibration_table(samples, 20) if r["n"])
    assert row["n"] == 100
    assert abs(row["mean_price"] - 0.72) < 1e-9
    assert abs(row["yes_rate"] - 0.72) < 1e-9
    assert abs(row["edge"]) < 1e-9


def test_calibration_underpriced_favorite_positive_edge():
    samples = [(0.85, 1)] * 95 + [(0.85, 0)] * 5            # priced 0.85, hit 0.95
    row = next(r for r in calibration_table(samples, 20) if r["n"])
    assert abs(row["edge"] - 0.10) < 1e-9                   # +0.10 -> underpriced
    assert row["se"] and row["se"] > 0


def test_calibration_drops_malformed_rows():
    samples = [(0.5, 1), (None, 1), (0.5, 0), (1.5, 1), (0.5, 7)]
    row = next(r for r in calibration_table(samples, 10) if r["n"])
    assert row["n"] == 2                                    # only the two valid (0.5,*)


def test_calibration_empty_buckets_are_kept_but_null():
    table = calibration_table([(0.5, 1)], 10)
    assert any(r["n"] == 0 and r["edge"] is None for r in table)
    assert sum(r["n"] for r in table) == 1


# ── bias_summary (favorite-longshot signature) ───────────────────────────────

def test_bias_summary_detects_longshot_bias():
    # longshots (0.05) never hit; favorites (0.95) always hit.
    samples = [(0.05, 0)] * 100 + [(0.95, 1)] * 100
    b = bias_summary(calibration_table(samples, 20))
    assert b["cheap_lt_0.2"]["edge"] < 0                    # 0.05 priced, 0.0 realized
    assert b["dear_gt_0.8"]["edge"] > 0                     # 0.95 priced, 1.0 realized


def test_bias_summary_empty_when_no_extremes():
    # everything mid-range -> no cheap/dear buckets populated.
    b = bias_summary(calibration_table([(0.5, 1), (0.5, 0)], 20))
    assert b["cheap_lt_0.2"]["n"] == 0
    assert b["dear_gt_0.8"]["n"] == 0
