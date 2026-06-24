"""Tests for the counterfactual exit-quality core in scripts/exit_quality.py
(the pure decision; the mark fetch + printing are thin I/O)."""

from __future__ import annotations

from scripts.exit_quality import exit_assessment, outcome_label


def test_sold_winner_that_kept_rising_left_money_on_table():
    # Bought .40, sold .60 (+20 booked on 100 sh), but it ran to .90.
    # Holding would have made +50; selling left -30 on the table.
    a = exit_assessment(avg_in=0.40, avg_out=0.60, mark=0.90, shares=100)
    assert round(a["realized"], 6) == 20.0
    assert round(a["hold_pnl"], 6) == 50.0
    assert round(a["edge"], 6) == -30.0          # negative = sold too early


def test_sold_before_a_collapse_saved_money():
    # Bought .40, sold .60, then it cratered to .05. Holding = -35; selling
    # locked +20. The exit SAVED +55 vs holding.
    a = exit_assessment(avg_in=0.40, avg_out=0.60, mark=0.05, shares=100)
    assert round(a["realized"], 6) == 20.0
    assert round(a["hold_pnl"], 6) == -35.0
    assert round(a["edge"], 6) == 55.0           # positive = scalp worked


def test_cut_a_loss_that_then_won_is_worst_case():
    # Bought .60, panic-sold .40 (-20), but it WON (mark ~1). Holding = +40;
    # cutting cost us -60 of edge.
    a = exit_assessment(avg_in=0.60, avg_out=0.40, mark=0.99, shares=100)
    assert round(a["realized"], 6) == -20.0
    assert round(a["edge"], 2) == -59.0


def test_edge_is_sold_minus_mark_times_shares():
    # Identity: edge == (avg_out - mark) * shares, independent of avg_in.
    a = exit_assessment(avg_in=0.123, avg_out=0.50, mark=0.30, shares=10)
    assert round(a["edge"], 6) == round((0.50 - 0.30) * 10, 6)


def test_outcome_label_buckets():
    assert outcome_label(0.999) == "won"
    assert outcome_label(0.95) == "won"
    assert outcome_label(0.001) == "lost"
    assert outcome_label(0.05) == "lost"
    assert outcome_label(0.5) == "live"
    assert outcome_label(-1.0) == "unknown"
