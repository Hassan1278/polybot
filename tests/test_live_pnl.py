"""Tests for the venue-truth realized-PnL reconstruction in scripts/live_pnl.py.

The fetch/print parts are thin I/O over the data API; the testable core is the
pure average-cost accounting (`realized_from_activity`) and the row normaliser
(`_norm`).
"""

from __future__ import annotations

from scripts.live_pnl import _norm, cashflow_totals, realized_from_activity


def _ev(typ, side, shares, usdc, asset="T1", ts=0, title="Mkt", outcome="Yes"):
    return {"type": typ, "side": side, "shares": shares, "usdc": usdc,
            "price": (usdc / shares if shares else 0.0),
            "asset": asset, "title": title, "outcome": outcome, "ts": ts}


def test_norm_fills_usdc_from_size_times_price():
    # usdcSize absent -> derive from size*price so notional is never read as 0.
    out = _norm({"type": "TRADE", "side": "BUY", "size": 10, "price": 0.30})
    assert out["usdc"] == 3.0
    # asset falls back asset -> conditionId -> "" (string, never None).
    assert out["asset"] == ""
    assert _norm({"conditionId": "0xabc"})["asset"] == "0xabc"


def test_norm_prefers_explicit_usdc():
    out = _norm({"type": "TRADE", "side": "SELL", "size": 10, "price": 0.30,
                 "usdcSize": 5.0, "asset": "tok"})
    assert out["usdc"] == 5.0
    assert out["asset"] == "tok"


def test_simple_round_trip_win():
    # Buy 100 @ .40 (cost 40), sell 100 @ .60 (proceeds 60) -> +20 realized.
    book = realized_from_activity([
        _ev("TRADE", "BUY", 100, 40.0, ts=1),
        _ev("TRADE", "SELL", 100, 60.0, ts=2),
    ])
    s = book["T1"]
    assert round(s["sold_realized"], 6) == 20.0
    assert round(s["avg_in"], 3) == 0.400
    assert round(s["avg_out"], 3) == 0.600
    assert s["open_shares"] == 0.0


def test_simple_round_trip_loss():
    book = realized_from_activity([
        _ev("TRADE", "BUY", 100, 50.0, ts=1),
        _ev("TRADE", "SELL", 100, 35.0, ts=2),
    ])
    s = book["T1"]
    assert round(s["sold_realized"], 6) == -15.0
    assert s["open_shares"] == 0.0


def test_partial_sell_leaves_open_lot_at_avg_cost():
    # Buy 100 @ .40, sell 40 @ .50: realized = 40*(.50-.40)=+4; 60 sh left @ .40.
    book = realized_from_activity([
        _ev("TRADE", "BUY", 100, 40.0, ts=1),
        _ev("TRADE", "SELL", 40, 20.0, ts=2),
    ])
    s = book["T1"]
    assert round(s["sold_realized"], 6) == 4.0
    assert round(s["open_shares"], 6) == 60.0
    assert round(s["open_cost"], 6) == 24.0          # 60 * .40


def test_average_cost_across_two_buys():
    # Buy 100 @ .20 then 100 @ .40 -> avg .30 over 200 sh. Sell 200 @ .35:
    # realized = 200*(.35-.30) = +10.
    book = realized_from_activity([
        _ev("TRADE", "BUY", 100, 20.0, ts=1),
        _ev("TRADE", "BUY", 100, 40.0, ts=2),
        _ev("TRADE", "SELL", 200, 70.0, ts=3),
    ])
    s = book["T1"]
    assert round(s["avg_in"], 4) == 0.30
    assert round(s["sold_realized"], 6) == 10.0


def test_redeem_accounts_against_avg_cost():
    # Buy 100 @ .45 (cost 45), held to resolution, redeemed for $100 (winner).
    book = realized_from_activity([
        _ev("TRADE", "BUY", 100, 45.0, ts=1),
        _ev("REDEEM", "", 100, 100.0, ts=2),
    ])
    s = book["T1"]
    assert round(s["redeemed_realized"], 6) == 55.0
    assert s["sold_realized"] == 0.0
    assert s["open_shares"] == 0.0


def test_events_replayed_in_timestamp_order_not_list_order():
    # SELL listed before its BUY (newest-first feed) must still net correctly.
    book = realized_from_activity([
        _ev("TRADE", "SELL", 100, 60.0, ts=2),
        _ev("TRADE", "BUY", 100, 40.0, ts=1),
    ])
    assert round(book["T1"]["sold_realized"], 6) == 20.0


def test_distinct_tokens_are_independent():
    book = realized_from_activity([
        _ev("TRADE", "BUY", 100, 40.0, asset="A", ts=1),
        _ev("TRADE", "SELL", 100, 60.0, asset="A", ts=2),
        _ev("TRADE", "BUY", 50, 30.0, asset="B", ts=3),
        _ev("TRADE", "SELL", 50, 10.0, asset="B", ts=4),
    ])
    assert round(book["A"]["sold_realized"], 6) == 20.0
    assert round(book["B"]["sold_realized"], 6) == -20.0


def test_oversell_guard_never_goes_negative_shares():
    # A stray SELL bigger than what's held (feed gap) caps at held, no crash.
    book = realized_from_activity([
        _ev("TRADE", "BUY", 50, 20.0, ts=1),
        _ev("TRADE", "SELL", 100, 60.0, ts=2),
    ])
    s = book["T1"]
    assert s["open_shares"] == 0.0
    assert s["sold_shares"] == 50.0


# ── cashflow_totals (double-count-proof account total) ───────────────────────

def test_cashflow_totals_sums_by_type():
    cf = cashflow_totals([
        _ev("TRADE", "BUY", 100, 40.0),
        _ev("TRADE", "BUY", 50, 30.0),
        _ev("TRADE", "SELL", 80, 60.0),
        _ev("REDEEM", "", 70, 70.0),
    ])
    assert cf["buys"] == 70.0
    assert cf["sells"] == 60.0
    assert cf["redeems"] == 70.0


def test_cashflow_total_matches_real_pnl_despite_redeem_key_mismatch():
    # The venue logs a winning REDEEM under a DIFFERENT token id than the BUY.
    # Per-token matching would split it into a phantom loss + a cost-0 win, but
    # the cashflow net = sells + redeems + open_value - buys is still correct.
    events = [
        _ev("TRADE", "BUY", 100, 45.0, asset="BUY_TOKEN"),   # paid 45
        _ev("REDEEM", "", 100, 100.0, asset="REDEEM_TOKEN"),  # got 100 (won)
    ]
    cf = cashflow_totals(events)
    net = cf["sells"] + cf["redeems"] + 0.0 - cf["buys"]
    assert round(net, 6) == 55.0          # true PnL, no double count


def test_cashflow_ignores_nonpositive_notional():
    cf = cashflow_totals([
        _ev("TRADE", "BUY", 0, 0.0),
        _ev("TRADE", "SELL", 10, 5.0),
    ])
    assert cf["buys"] == 0.0
    assert cf["sells"] == 5.0
