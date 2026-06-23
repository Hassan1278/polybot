"""Tests for the same-day crypto price-bucket guard:
`services/executor/risk._same_day_bucket_conflict`.

The rule: at most ONE "between $A-$B" price-bucket position per crypto asset per
UTC resolution day. A new range bet may not open a DIFFERENT band on the same
asset+day as an already-open range position. Same band (daily reissue / same
market), different day, different asset, and non-range markets all fail open
(return None).

Like the crypto-timeframe test we feed the helper a fake async session that
replays a queue of results, so the tests run without Postgres/Redis. The helper
issues at most two queries:
  1) the incoming market row   -> .first() -> (question, slug, category, end_date)
  2) the held open-crypto legs -> .all()   -> list of row tuples
(query #2 is skipped when the incoming market is already non-qualifying.)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from services.executor import risk as risk_mod

# Future resolution days (open markets); the .date() comparison is what matters.
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


def _btc(lo, hi, day="June 21"):
    """A daily BTC price-bucket (range) market question + slug."""
    return (f"Will Bitcoin be between ${lo:,} and ${hi:,} on {day}?",
            f"btc-{lo}-{hi}-{day.replace(' ', '').lower()}")


def _btc_above(level, day="June 21"):
    """A daily BTC threshold market question + slug."""
    return (f"Will Bitcoin be above ${level:,} on {day}?",
            f"btc-above-{level}-{day.replace(' ', '').lower()}")


def _run(sess, *, mode="paper", market_id, outcome, side="BUY"):
    return asyncio.run(risk_mod._same_day_bucket_conflict(
        sess, mode=mode, market_id=market_id, outcome=outcome, side=side))


# ── conflicts ────────────────────────────────────────────────────────────────

def test_different_bucket_same_day_conflicts():
    # Hold NO 62-64k June 21; new NO 64-66k June 21 -> blocked (the reported case).
    hq, hslug = _btc(62000, 64000)
    iq, islug = _btc(64000, 66000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _held_paper([(hq, hslug, "No", D21, "BTC_62_64")]),
    ])
    res = _run(sess, market_id="BTC_64_66", outcome="No", side="BUY")
    assert res is not None
    asset, band, held_mid = res
    assert asset == "BTC"
    assert band == "64000-66000"
    assert held_mid == "BTC_62_64"


def test_contradictory_yes_buckets_conflict():
    # Two YES buckets the same day is a literal contradiction -> blocked.
    hq, hslug = _btc(62000, 64000)
    iq, islug = _btc(64000, 66000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _held_paper([(hq, hslug, "Yes", D21, "BTC_62_64")]),
    ])
    res = _run(sess, market_id="BTC_64_66", outcome="Yes", side="BUY")
    assert res is not None and res[0] == "BTC" and res[2] == "BTC_62_64"


def test_threshold_vs_range_conflicts():
    # The reported bug: hold "above 62k NO" (wins <=62k); new "between 60-62k NO"
    # (wins <60k OR >62k) same day -> their win-regions cross -> blocked.
    hq, hslug = _btc_above(62000)
    iq, islug = _btc(60000, 62000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _held_paper([(hq, hslug, "No", D21, "BTC_ABOVE_62")]),
    ])
    res = _run(sess, market_id="BTC_60_62", outcome="No", side="BUY")
    assert res is not None and res[0] == "BTC" and res[2] == "BTC_ABOVE_62"


def test_range_vs_threshold_conflicts_reverse_order():
    # Same conflict with the legs swapped: hold the range, new order is the threshold.
    hq, hslug = _btc(60000, 62000)
    iq, islug = _btc_above(62000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _held_paper([(hq, hslug, "No", D21, "BTC_60_62")]),
    ])
    res = _run(sess, market_id="BTC_ABOVE_62", outcome="No", side="BUY")
    assert res is not None and res[0] == "BTC" and res[2] == "BTC_60_62"


def test_nested_thresholds_allowed():
    # "above 60k NO" (wins <=60k) is NESTED inside "above 62k NO" (wins <=62k):
    # same bearish thesis, a refinement -> not a conflict.
    hq, hslug = _btc_above(60000)
    iq, islug = _btc_above(62000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _held_paper([(hq, hslug, "No", D21, "BTC_ABOVE_60")]),
    ])
    assert _run(sess, market_id="BTC_ABOVE_62", outcome="No", side="BUY") is None


def test_live_mode_conflict():
    # Live held rows carry an extra `side` column; the band comparison still holds.
    hq, hslug = _btc(62000, 64000)
    iq, islug = _btc(64000, 66000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _Res(all_rows=[(hq, hslug, "No", "BUY", D21, "BTC_62_64")]),
    ])
    res = asyncio.run(risk_mod._same_day_bucket_conflict(
        sess, mode="live", market_id="BTC_64_66", outcome="No", side="BUY"))
    assert res is not None and res[0] == "BTC" and res[2] == "BTC_62_64"


# ── allowed (no conflict) ────────────────────────────────────────────────────

def test_same_bucket_allowed():
    # Same band (daily reissue / adding to the bucket) -> not a different-bucket conflict.
    hq, hslug = _btc(62000, 64000)
    iq, islug = _btc(62000, 64000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _held_paper([(hq, hslug, "No", D21, "BTC_62_64_OLD")]),
    ])
    assert _run(sess, market_id="BTC_62_64_NEW", outcome="No", side="BUY") is None


def test_different_day_allowed():
    # New 64-66k resolves June 22; held 62-64k resolves June 21 -> separate book.
    hq, hslug = _btc(62000, 64000, "June 21")
    iq, islug = _btc(64000, 66000, "June 22")
    sess = _Sess([
        _incoming(iq, islug, D22),
        _held_paper([(hq, hslug, "No", D21, "BTC_62_64")]),
    ])
    assert _run(sess, market_id="BTC_64_66", outcome="No", side="BUY") is None


def test_different_asset_allowed():
    # Incoming BTC bucket; the only open leg is an ETH bucket -> different asset.
    hq = "Will Ethereum be between $3,000 and $3,200 on June 21?"
    iq, islug = _btc(64000, 66000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _held_paper([(hq, "eth-3000-3200-june21", "No", D21, "ETH_MKT")]),
    ])
    assert _run(sess, market_id="BTC_64_66", outcome="No", side="BUY") is None


def test_incoming_non_range_fails_open():
    # A directional "Up or Down" market isn't a range bet -> no constraint (and the
    # helper returns before querying held legs, so only one result is queued).
    sess = _Sess([
        _incoming("Bitcoin Up or Down on June 21?", "btc-updown-0621", D21),
    ])
    assert _run(sess, market_id="BTC_MKT", outcome="Down", side="BUY") is None


def test_held_non_range_allowed():
    # Incoming is a bucket; the only open leg is directional (not a range) -> allowed.
    iq, islug = _btc(64000, 66000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _held_paper([("Bitcoin Up or Down on June 21?", "btc-updown-0621", "Up", D21, "BTC_DIR")]),
    ])
    assert _run(sess, market_id="BTC_64_66", outcome="No", side="BUY") is None


def test_non_crypto_exempt():
    # Non-crypto category returns before any parsing -> only one result queued.
    sess = _Sess([
        _incoming("Will the daily high be between 60 and 62 degrees on June 21?",
                  "weather-60-62-0621", D21, category="weather"),
    ])
    assert _run(sess, market_id="WX_MKT", outcome="No", side="BUY") is None


def test_same_market_id_skipped():
    # The only sibling band is the SAME market we're entering -> handled elsewhere.
    hq, _ = _btc(62000, 64000)
    iq, islug = _btc(64000, 66000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _held_paper([(hq, "btc-62-64", "No", D21, "BTC_64_66")]),
    ])
    assert _run(sess, market_id="BTC_64_66", outcome="No", side="BUY") is None


def test_missing_end_date_fails_open():
    iq, islug = _btc(64000, 66000)
    sess = _Sess([
        _incoming(iq, islug, None),
    ])
    assert _run(sess, market_id="BTC_64_66", outcome="No", side="BUY") is None


def test_held_missing_end_date_skipped():
    # A held leg with no end_date can't be day-matched -> skipped (fail open).
    hq, hslug = _btc(62000, 64000)
    iq, islug = _btc(64000, 66000)
    sess = _Sess([
        _incoming(iq, islug, D21),
        _held_paper([(hq, hslug, "No", None, "BTC_62_64")]),
    ])
    assert _run(sess, market_id="BTC_64_66", outcome="No", side="BUY") is None
