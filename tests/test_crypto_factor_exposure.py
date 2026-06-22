"""Tests for the crypto factor-exposure cap:
`services/executor/risk._crypto_factor_exposure`.

The rule: treat all crypto MAJORS as ONE bet per direction and cap the GROSS
same-direction notional across every crypto market. A new directional crypto bet
is blocked if existing same-direction crypto notional + this order exceeds the
cap. Opposite-direction legs, memecoins, non-crypto, and non-directional
(range-only) markets don't count / fail open.

Fake async session replays a queue of results (no Postgres/Redis). The helper
issues at most two queries:
  1) incoming market row   -> .first() -> (question, slug, category)
  2) held open-crypto legs -> .all()   -> list of row tuples
(query #2 is skipped when the incoming market is already non-qualifying.)
"""

from __future__ import annotations

import asyncio

from services.executor import risk as risk_mod


# ── fakes ────────────────────────────────────────────────────────────────────

class _Res:
    def __init__(self, first=None, all_rows=None):
        self._first = first
        self._all = all_rows or []

    def first(self):
        return self._first

    def all(self):
        return self._all


class _Sess:
    """Replays a pre-supplied queue of _Res objects, one per execute()."""

    def __init__(self, results):
        self._q = list(results)

    async def execute(self, *_args, **_kwargs):
        return self._q.pop(0)


def _incoming(question, slug, category="crypto"):
    return _Res(first=(question, slug, category))


def _held_paper(rows):
    # rows: list of (market_id, question, slug, outcome, notional)
    return _Res(all_rows=rows)


def _run(sess, *, mode="paper", market_id, outcome, side="BUY", size_usdc, cap):
    return asyncio.run(risk_mod._crypto_factor_exposure(
        sess, mode=mode, market_id=market_id, outcome=outcome, side=side,
        size_usdc=size_usdc, cap=cap))


# ── blocked ──────────────────────────────────────────────────────────────────

def test_same_direction_crypto_sum_exceeds_cap_blocks():
    # Hold $40 BTC-bear + $40 ETH-bear; new $30 BTC-bear; cap 100 -> 80+30 > 100.
    sess = _Sess([
        _incoming("Will Bitcoin fall below $60,000 on June 21?", "btc-below-60-0621"),
        _held_paper([
            ("BTC1", "Will Bitcoin fall below $60,000 on June 20?", "btc-0620", "Yes", 40.0),
            ("ETH1", "Will Ethereum drop below $3,000 on June 20?", "eth-0620", "Yes", 40.0),
        ]),
    ])
    res = _run(sess, market_id="BTC2", outcome="Yes", side="BUY", size_usdc=30.0, cap=100.0)
    assert res is not None
    want_dir, total = res
    assert want_dir == "bear"
    assert total == 80.0


def test_cross_asset_majors_sum_one_factor():
    # BTC + ETH + SOL bear all count toward the same directional factor.
    sess = _Sess([
        _incoming("Will Bitcoin fall below $60,000 today?", "btc-x"),
        _held_paper([
            ("ETH1", "Will Ethereum drop below $3,000 today?", "eth", "Yes", 30.0),
            ("SOL1", "Will Solana fall below $120 today?", "sol", "Yes", 30.0),
        ]),
    ])
    res = _run(sess, market_id="BTC2", outcome="Yes", side="BUY", size_usdc=50.0, cap=100.0)
    assert res is not None and res[1] == 60.0      # 60 held + 50 new > 100


# ── allowed ──────────────────────────────────────────────────────────────────

def test_under_cap_allowed():
    sess = _Sess([
        _incoming("Will Bitcoin fall below $60,000 today?", "btc"),
        _held_paper([("ETH1", "Will Ethereum drop below $3,000 today?", "eth", "Yes", 40.0)]),
    ])
    assert _run(sess, market_id="BTC2", outcome="Yes", side="BUY", size_usdc=15.0, cap=100.0) is None


def test_opposite_direction_not_summed():
    # Held bear; incoming bull -> different factor leg -> total 0 -> allowed.
    sess = _Sess([
        _incoming("Will Bitcoin rise above $70,000 today?", "btc-up"),
        _held_paper([("ETH1", "Will Ethereum drop below $3,000 today?", "eth", "Yes", 90.0)]),
    ])
    assert _run(sess, market_id="BTC2", outcome="Yes", side="BUY", size_usdc=20.0, cap=100.0) is None


def test_sell_flips_direction():
    # SELL a "fall below" YES is bullish -> doesn't sum with a held bear leg.
    sess = _Sess([
        _incoming("Will Bitcoin fall below $60,000 today?", "btc"),
        _held_paper([("ETH1", "Will Ethereum drop below $3,000 today?", "eth", "Yes", 90.0)]),
    ])
    assert _run(sess, market_id="BTC2", outcome="Yes", side="SELL", size_usdc=20.0, cap=100.0) is None


def test_incoming_memecoin_exempt():
    # DOGE isn't a major -> returns before querying held legs (one result queued).
    sess = _Sess([
        _incoming("Will Dogecoin fall below $0.10 today?", "doge"),
    ])
    assert _run(sess, market_id="DOGE", outcome="Yes", side="BUY", size_usdc=99.0, cap=100.0) is None


def test_incoming_non_directional_range_fails_open():
    # "between $A and $B" has no bull/bear direction -> no factor constraint.
    sess = _Sess([
        _incoming("Will Bitcoin be between $62,000 and $64,000 today?", "btc-range"),
    ])
    assert _run(sess, market_id="BTC", outcome="No", side="BUY", size_usdc=99.0, cap=100.0) is None


def test_non_crypto_exempt():
    sess = _Sess([
        _incoming("Will the Fed cut rates in July?", "fed", category="macro"),
    ])
    assert _run(sess, market_id="MACRO", outcome="Yes", side="BUY", size_usdc=99.0, cap=100.0) is None


def test_held_memecoin_not_summed():
    # Incoming BTC bear; only open same-dir leg is DOGE (non-major) -> total 0.
    sess = _Sess([
        _incoming("Will Bitcoin fall below $60,000 today?", "btc"),
        _held_paper([("DOGE1", "Will Dogecoin fall below $0.10 today?", "doge", "Yes", 90.0)]),
    ])
    assert _run(sess, market_id="BTC2", outcome="Yes", side="BUY", size_usdc=20.0, cap=100.0) is None


def test_live_mode_sums():
    # Live held rows carry an extra `side` column; bear total still accrues.
    sess = _Sess([
        _incoming("Will Bitcoin fall below $60,000 today?", "btc"),
        _Res(all_rows=[
            ("ETH1", "Will Ethereum drop below $3,000 today?", "eth", "Yes", "BUY", 70.0),
        ]),
    ])
    res = asyncio.run(risk_mod._crypto_factor_exposure(
        sess, mode="live", market_id="BTC2", outcome="Yes", side="BUY",
        size_usdc=40.0, cap=100.0))
    assert res is not None and res[0] == "bear" and res[1] == 70.0
