"""Tests for the pure side-bias localization in scripts/side_bias.py
(outcome normalization, NO-share, and the where-does-it-enter verdict).
The DB aggregation layers are thin SQL and exercised on the VPS."""

from __future__ import annotations

from scripts.side_bias import _norm_outcome, locate_skew, no_pct


def test_norm_outcome_buckets():
    assert _norm_outcome("Yes") == "YES"
    assert _norm_outcome("NO") == "NO"
    assert _norm_outcome("no") == "NO"
    assert _norm_outcome("Donald Trump") == "OTHER"   # multi-outcome label
    assert _norm_outcome(None) == "OTHER"


def test_no_pct_basic():
    assert no_pct(0, 100) == 1.0
    assert no_pct(50, 50) == 0.5
    assert no_pct(0, 0) is None                        # no YES/NO volume


def test_skew_already_in_input_means_mirroring():
    # NO-heavy from the very first stage -> mirroring real flow, not a bug.
    v = locate_skew(0.70, 0.72, 0.95)
    assert "mirroring real sharp flow" in v
    assert "ALREADY in" in v


def test_skew_enters_at_clustering():
    # Balanced input, NO-heavy signals -> the clustering introduces it.
    v = locate_skew(0.50, 0.82, 0.95)
    assert "ENTERS at signal generation" in v


def test_skew_enters_at_execution():
    # Balanced input AND signals, NO-heavy only at entries -> execution/gating.
    v = locate_skew(0.50, 0.52, 0.90)
    assert "ENTERS at entry execution" in v


def test_skew_none_when_entries_balanced():
    v = locate_skew(0.50, 0.50, 0.50)
    assert "aren't materially NO-leaning" in v


def test_skew_handles_missing_upstream():
    # Only fills present (no sharp/signal data) -> say so, don't fabricate.
    v = locate_skew(None, None, 0.90)
    assert "Only entry data available" in v


def test_skew_no_entry_data():
    assert "can't assess" in locate_skew(0.6, 0.6, None)


def test_skew_diffuse_when_no_single_jump():
    # Creeps up <12pt per stage -> diffuse, no single culprit.
    v = locate_skew(0.52, 0.58, 0.64)
    assert "accumulates gradually" in v
