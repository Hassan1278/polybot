"""Tests for the short-dated crypto exits (services/executor/exit_rules.py):
the pure price/sentiment decisions, the asset-sentiment aggregation, and the
_evaluate_rules orchestration (close path + dust skip). DB and CLOB are
monkeypatched so logic is tested in isolation, mirroring test_exit_decision.py.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import services.executor.exit_rules as er
from services.executor.exit_rules import price_exit_reason, sentiment_breached

_CFG = {
    "take_profit_pct": 0.50, "stop_loss_pct": 0.50,
    "thesis_invalidation_mark": 0.20, "flatten_before_expiry_hours": 1.0,
    "min_close_notional_usdc": 2.0, "price_live_enabled": True,
    "sentiment_against_threshold": 0.65, "sentiment_min_sharps": 5,
    "sentiment_live_enabled": False, "cooldown_seconds": 300,
}


# ── pure: price_exit_reason ──────────────────────────────────────────────────

def test_price_take_profit():
    assert price_exit_reason(avg_entry=0.40, mark=0.70, hrs_to_expiry=10, cfg=_CFG) == "take_profit"


def test_price_take_profit_wins_over_near_expiry():
    # A leg that has run is locked in even if it's also near expiry.
    assert price_exit_reason(avg_entry=0.40, mark=0.70, hrs_to_expiry=0.5, cfg=_CFG) == "take_profit"


def test_price_thesis_invalidated_before_stop_loss():
    # mark <= 0.20 floor fires as thesis_invalidated, checked before stop_loss.
    assert price_exit_reason(avg_entry=0.50, mark=0.15, hrs_to_expiry=10, cfg=_CFG) == "thesis_invalidated"


def test_price_stop_loss():
    cfg = {**_CFG, "thesis_invalidation_mark": 0.10}   # below mark, so SL is the trigger
    assert price_exit_reason(avg_entry=0.50, mark=0.20, hrs_to_expiry=10, cfg=cfg) == "stop_loss"


def test_price_near_expiry():
    assert price_exit_reason(avg_entry=0.50, mark=0.52, hrs_to_expiry=0.5, cfg=_CFG) == "near_expiry"


def test_price_no_trigger():
    assert price_exit_reason(avg_entry=0.50, mark=0.55, hrs_to_expiry=10, cfg=_CFG) is None


def test_price_guards_unpriceable():
    assert price_exit_reason(avg_entry=0.50, mark=0.0, hrs_to_expiry=10, cfg=_CFG) is None
    assert price_exit_reason(avg_entry=0.0, mark=0.50, hrs_to_expiry=10, cfg=_CFG) is None


def test_price_empty_cfg_is_noop():
    assert price_exit_reason(avg_entry=0.40, mark=0.99, hrs_to_expiry=0.0, cfg={}) is None


# ── pure: sentiment_breached ─────────────────────────────────────────────────

def test_sentiment_fires():
    assert sentiment_breached(weighted_against=0.70, n_sharps=6, stats_fresh=True, cfg=_CFG)


def test_sentiment_stale_blocks():
    assert not sentiment_breached(weighted_against=0.90, n_sharps=20, stats_fresh=False, cfg=_CFG)


def test_sentiment_thin_blocks():
    assert not sentiment_breached(weighted_against=0.90, n_sharps=3, stats_fresh=True, cfg=_CFG)


def test_sentiment_below_threshold():
    assert not sentiment_breached(weighted_against=0.60, n_sharps=10, stats_fresh=True, cfg=_CFG)


# ── _asset_sharp_sentiment aggregation ───────────────────────────────────────

class _Res:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Sess:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *a, **k):
        return _Res(self._rows)


def test_asset_sentiment_weighted_against(monkeypatch):
    # Two BTC sharps: 0xa net-BULL (weight .8), 0xb net-BEAR (weight .2). An ETH
    # row is excluded. We hold BEAR, so "against" = bull = 0.8 / (0.8+0.2) = 0.8.
    rows = [
        ("0xa", "YES", "BUY", 100.0, "will btc be above $64000", ""),
        ("0xb", "NO", "BUY", 100.0, "will btc be above $64000", ""),
        ("0xc", "YES", "BUY", 500.0, "will eth be above $4000", ""),
    ]

    async def _wf(_s, wallets, _cfg):
        return ({"0xa": 0.8, "0xb": 0.2, "0xc": 0.9}, True)

    monkeypatch.setattr(er, "_weights_fresh", _wf)
    frac, n, fresh = asyncio.run(
        er._asset_sharp_sentiment(_Sess(rows), "BTC", "bear", _CFG))
    assert abs(frac - 0.8) < 1e-9 and n == 2 and fresh is True


def test_asset_sentiment_no_stances_is_zero(monkeypatch):
    async def _wf(_s, wallets, _cfg):  # pragma: no cover - shouldn't be called
        return ({}, True)

    monkeypatch.setattr(er, "_weights_fresh", _wf)
    frac, n, fresh = asyncio.run(
        er._asset_sharp_sentiment(_Sess([]), "BTC", "bear", _CFG))
    assert frac == 0.0 and n == 0 and fresh is True


# ── _evaluate_rules orchestration ────────────────────────────────────────────

def _patch_rules(monkeypatch, *, view, sentiment=(0.0, 0, True)):
    calls: dict = {"close": []}

    @asynccontextmanager
    async def _scope():
        yield object()

    async def _pv(_s, _clob, _m, _o):
        return view

    async def _sent(_s, _asset, _dir, _cfg):
        return sentiment

    async def _close(market_id, outcome, *, notes, live_ok, cooldown,
                     cluster=(), skip_event="exit_rule_skip_live"):
        calls["close"].append({"notes": notes, "live_ok": live_ok})

    monkeypatch.setattr(er, "session_scope", _scope)
    monkeypatch.setattr(er, "_position_view", _pv)
    monkeypatch.setattr(er, "_asset_sharp_sentiment", _sent)
    monkeypatch.setattr(er, "do_close", _close)
    return calls


def _view(**over):
    v = {"shares": 25.0, "notional": 10.0, "avg_entry": 0.40, "mark": 0.40,
         "asset": "BTC", "my_dir": "bear", "hrs": 10.0}
    v.update(over)
    return v


def test_evaluate_price_take_profit_closes_live(monkeypatch):
    calls = _patch_rules(monkeypatch, view=_view(mark=0.70))     # +75% → take_profit
    asyncio.run(er._evaluate_rules("M", "NO", None, cfg=_CFG, sentiment_cache={}))
    assert calls["close"] == [{"notes": "exit_take_profit", "live_ok": True}]


def test_evaluate_skips_dust(monkeypatch):
    calls = _patch_rules(monkeypatch, view=_view(mark=0.99, notional=1.0))   # below min
    asyncio.run(er._evaluate_rules("M", "NO", None, cfg=_CFG, sentiment_cache={}))
    assert calls["close"] == []


def test_evaluate_sentiment_shadow(monkeypatch):
    # No price trigger (small gain), but sharps net 80% against us → shadow close.
    calls = _patch_rules(monkeypatch, view=_view(mark=0.42), sentiment=(0.80, 6, True))
    asyncio.run(er._evaluate_rules("M", "NO", None, cfg=_CFG, sentiment_cache={}))
    assert calls["close"] == [{"notes": "exit_sentiment", "live_ok": False}]


def test_evaluate_no_trigger_no_close(monkeypatch):
    calls = _patch_rules(monkeypatch, view=_view(mark=0.42), sentiment=(0.10, 6, True))
    asyncio.run(er._evaluate_rules("M", "NO", None, cfg=_CFG, sentiment_cache={}))
    assert calls["close"] == []
