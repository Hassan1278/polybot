"""Tests for the cross-asset crypto directional guard:
`services/executor/risk._crypto_timeframe_conflict`.

The rule: among correlated crypto MAJORS resolving on the SAME UTC day, a new
bet may not oppose the direction of an already-open major-crypto leg. Memecoins
are exempt, different resolution days are separate books, and anything ambiguous
fails open (returns None).

We feed the helper a fake async session that replays a queue of results, so the
tests run without Postgres/Redis. The helper issues at most two queries:
  1) the incoming market row  -> .first() -> (question, slug, category, end_date)
  2) the held open-crypto legs -> .all()  -> list of row tuples
(query #2 is skipped when the incoming market is already non-qualifying.)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from services.executor import risk as risk_mod

# Future resolution days (env "today" is 2026-06-20, so these are open markets).
D21 = datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc)
D22 = datetime(2026, 6, 22, 20, 0, tzinfo=timezone.utc)


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


def _incoming(question, slug, end, category="crypto"):
    return _Res(first=(question, slug, category, end))


def _held_paper(rows):
    # rows: list of (question, slug, outcome, end_date, market_id)
    return _Res(all_rows=rows)


def _run(sess, *, mode="paper", market_id, outcome, side="BUY"):
    return asyncio.run(risk_mod._crypto_timeframe_conflict(
        sess, mode=mode, market_id=market_id, outcome=outcome, side=side))


# ── conflicts ────────────────────────────────────────────────────────────────

def test_opposing_major_same_day_conflicts():
    # Open BTC "Up" (bull) June 21; new ETH "Down" (bear) June 21 -> blocked.
    sess = _Sess([
        _incoming("Ethereum Up or Down on June 21?", "eth-updown-0621", D21),
        _held_paper([("Bitcoin Up or Down on June 21?", "btc-updown-0621", "Up", D21, "BTC_MKT")]),
    ])
    res = _run(sess, market_id="ETH_MKT", outcome="Down", side="BUY")
    assert res is not None
    asset, want_dir, have_asset, day = res
    assert asset == "ETH"
    assert want_dir == "bear"
    assert have_asset == "BTC"
    assert day == "2026-06-21"


def test_sell_flips_direction_and_conflicts():
    # SELL "Up" on ETH is a bearish bet; conflicts with an open BTC bull same day.
    sess = _Sess([
        _incoming("Ethereum Up or Down on June 21?", "eth-0621", D21),
        _held_paper([("Bitcoin Up or Down on June 21?", "btc-0621", "Up", D21, "BTC_MKT")]),
    ])
    res = _run(sess, market_id="ETH_MKT", outcome="Up", side="SELL")
    assert res is not None and res[1] == "bear" and res[2] == "BTC"


def test_live_mode_conflict():
    # Live held rows carry an extra `side` column; the bear/bull comparison holds.
    sess = _Sess([
        _incoming("Ethereum Up or Down on June 21?", "eth-0621", D21),
        _Res(all_rows=[("Bitcoin Up or Down on June 21?", "btc-0621", "Up", "BUY", D21, "BTC_MKT")]),
    ])
    res = asyncio.run(risk_mod._crypto_timeframe_conflict(
        sess, mode="live", market_id="ETH_MKT", outcome="Down", side="BUY"))
    assert res is not None and res[0] == "ETH" and res[2] == "BTC"


# ── allowed (no conflict) ────────────────────────────────────────────────────

def test_same_direction_allowed():
    sess = _Sess([
        _incoming("Ethereum Up or Down on June 21?", "eth-0621", D21),
        _held_paper([("Bitcoin Up or Down on June 21?", "btc-0621", "Up", D21, "BTC_MKT")]),
    ])
    assert _run(sess, market_id="ETH_MKT", outcome="Up", side="BUY") is None


def test_different_day_allowed():
    # New ETH bear resolves June 22; open BTC bull resolves June 21 -> separate book.
    sess = _Sess([
        _incoming("Ethereum Up or Down on June 22?", "eth-0622", D22),
        _held_paper([("Bitcoin Up or Down on June 21?", "btc-0621", "Up", D21, "BTC_MKT")]),
    ])
    assert _run(sess, market_id="ETH_MKT", outcome="Down", side="BUY") is None


def test_incoming_memecoin_exempt():
    # DOGE is not a major -> helper returns before even querying held legs.
    sess = _Sess([
        _incoming("Dogecoin Up or Down on June 21?", "doge-0621", D21),
    ])
    assert _run(sess, market_id="DOGE_MKT", outcome="Down", side="BUY") is None


def test_held_memecoin_exempt():
    # Incoming ETH bear; the only open opposite leg is DOGE (non-major) -> allowed.
    sess = _Sess([
        _incoming("Ethereum Up or Down on June 21?", "eth-0621", D21),
        _held_paper([("Dogecoin Up or Down on June 21?", "doge-0621", "Up", D21, "DOGE_MKT")]),
    ])
    assert _run(sess, market_id="ETH_MKT", outcome="Down", side="BUY") is None


def test_non_crypto_exempt():
    sess = _Sess([
        _incoming("Will Trump win the 2028 nomination?", "trump-2028", D21, category="politics"),
    ])
    assert _run(sess, market_id="POL_MKT", outcome="Yes", side="BUY") is None


def test_ambiguous_range_market_fails_open():
    # A "between $A and $B" market has no bull/bear direction -> no constraint.
    sess = _Sess([
        _incoming("Will Bitcoin be between $62,000 and $64,000 on June 21?",
                  "btc-range-0621", D21),
    ])
    assert _run(sess, market_id="BTC_RANGE", outcome="No", side="BUY") is None


def test_same_market_id_skipped():
    # The only opposite leg is the SAME market we're entering -> handled elsewhere.
    sess = _Sess([
        _incoming("Ethereum Up or Down on June 21?", "eth-0621", D21),
        _held_paper([("Ethereum Up or Down on June 21?", "eth-0621", "Up", D21, "ETH_MKT")]),
    ])
    assert _run(sess, market_id="ETH_MKT", outcome="Down", side="BUY") is None


def test_missing_end_date_fails_open():
    sess = _Sess([
        _incoming("Ethereum Up or Down on June 21?", "eth-0621", None),
    ])
    assert _run(sess, market_id="ETH_MKT", outcome="Down", side="BUY") is None


# ── majors-set sanity ────────────────────────────────────────────────────────

def test_crypto_majors_subset_and_excludes_memecoins():
    from polybot.asset_direction import _ASSETS, CRYPTO_MAJORS

    # Every major must be a known asset so asset_of() can actually return it.
    assert CRYPTO_MAJORS.issubset(_ASSETS)
    # Memecoins are intentionally exempt; core majors are present.
    for meme in ("DOGE", "SHIB", "PEPE"):
        assert meme not in CRYPTO_MAJORS
    for core in ("BTC", "ETH", "SOL"):
        assert core in CRYPTO_MAJORS
