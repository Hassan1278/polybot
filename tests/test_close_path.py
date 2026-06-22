"""Tests for the exit-mirror close path (Part 2): `services/executor/close`.

Focus: the naked-short protections in close_live — it sizes a sell from the
venue-held shares (ground truth), floors so it can never request MORE than held,
and refuses to sell on dust / a failed venue read / a blocked close. The venue,
cancel, rate and order plumbing are monkeypatched so the orchestration/safety
logic is tested in isolation (DB-less).
"""

from __future__ import annotations

import asyncio

import services.executor.close as close_mod


def _setup(monkeypatch, *, held, cancelled=0, blocked=None):
    calls: dict = {}

    async def _cancel(market_id, outcome):
        calls["cancel"] = (market_id, outcome)
        return cancelled

    async def _held(market_id, outcome):
        return held

    async def _blocked(market_id):
        return blocked

    async def _place(*, signal_id, market_id, outcome, side, shares, order_kind="taker"):
        calls["place"] = {"shares": shares, "side": side, "order_kind": order_kind,
                          "signal_id": signal_id}
        return {"status": "submitted", "venue_order_id": "0xexit"}

    monkeypatch.setattr(close_mod, "_cancel_resting_buys", _cancel)
    monkeypatch.setattr(close_mod, "live_shares_held", _held)
    monkeypatch.setattr(close_mod, "_close_blocked_reason", _blocked)
    monkeypatch.setattr(close_mod, "place_live_shares", _place)
    return calls


def test_close_sells_floored_held(monkeypatch):
    calls = _setup(monkeypatch, held=10.0, cancelled=1)
    out = asyncio.run(close_mod.close_live(market_id="M", outcome="YES", signal_id=7))
    assert calls["place"]["shares"] == 10.0
    assert calls["place"]["side"] == "SELL"
    assert calls["place"]["order_kind"] == "taker"      # urgent default → crosses
    assert calls["place"]["signal_id"] == 7
    assert out["sold_shares"] == 10.0 and out["cancelled"] == 1


def test_close_clamp_floors_never_exceeds_held(monkeypatch):
    calls = _setup(monkeypatch, held=7.999)
    asyncio.run(close_mod.close_live(market_id="M", outcome="YES"))
    assert calls["place"]["shares"] == 7.99             # floored down
    assert calls["place"]["shares"] <= 7.999            # never more than held


def test_close_exact_min_sells(monkeypatch):
    calls = _setup(monkeypatch, held=5.0)
    asyncio.run(close_mod.close_live(market_id="M", outcome="YES"))
    assert calls["place"]["shares"] == 5.0


def test_close_below_min_cancels_only(monkeypatch):
    calls = _setup(monkeypatch, held=3.0, cancelled=2)  # < MIN_SHARES (5)
    out = asyncio.run(close_mod.close_live(market_id="M", outcome="YES"))
    assert "place" not in calls                         # never tried to sell
    assert out["status"] == "cancelled_only" and out["cancelled"] == 2


def test_close_below_min_no_position(monkeypatch):
    calls = _setup(monkeypatch, held=0.0, cancelled=0)
    out = asyncio.run(close_mod.close_live(market_id="M", outcome="YES"))
    assert "place" not in calls
    assert out["status"] == "no_position"


def test_close_venue_read_failure_does_not_sell(monkeypatch):
    calls = _setup(monkeypatch, held=None, cancelled=1)
    out = asyncio.run(close_mod.close_live(market_id="M", outcome="YES"))
    assert "place" not in calls                         # fail-safe: never sell blind
    assert out["status"] == "venue_read_failed"


def test_close_blocked_does_not_sell(monkeypatch):
    calls = _setup(monkeypatch, held=50.0, blocked="rate_limit_exit:6>=6")
    out = asyncio.run(close_mod.close_live(market_id="M", outcome="YES"))
    assert "place" not in calls
    assert out["status"] == "blocked" and "rate_limit_exit" in out["reason"]


def test_close_paper_wraps_position_close(monkeypatch):
    async def _cp(market_id, outcome, fraction=1.0):
        return {"status": "filled", "side": "SELL", "fraction": fraction}
    monkeypatch.setattr(close_mod.paper_mod, "close_position", _cp)
    out = asyncio.run(close_mod.close_paper(market_id="M", outcome="YES"))
    assert out["status"] == "filled" and out["fraction"] == 1.0
