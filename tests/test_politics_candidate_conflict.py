"""Tests for the one-position-per-politics-candidate guard:
`services/executor/risk._politics_candidate_held` and the
`packages/polybot/politics_candidate.candidate_of` extractor.

The rule: once we hold an open bet on a politics candidate (Trump excluded), a new
bet on that SAME candidate in a different market is refused — regardless of
direction. Anything ambiguous (no parseable name, non-politics, Trump) fails open.

The guard issues at most two queries against a fake async session:
  1) incoming market row -> .first() -> (question, slug, category)
  2) held open politics legs -> .all() -> [(question, slug, market_id), ...]
(query #2 is skipped when the incoming market doesn't name a usable candidate.)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from polybot.politics_candidate import POLITICS_CANDIDATE_EXCLUDE, candidate_of
from services.executor import risk as risk_mod

D21 = datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc)


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
    def __init__(self, results):
        self._q = list(results)

    async def execute(self, *_args, **_kwargs):
        return self._q.pop(0)


def _incoming(question, category="politics"):
    return _Res(first=(question, None, category))


def _held(rows):
    # rows: list of (question, slug, market_id)
    return _Res(all_rows=rows)


def _run(sess, *, mode="paper", market_id="M_NEW"):
    return asyncio.run(risk_mod._politics_candidate_held(
        sess, mode=mode, market_id=market_id))


# ── candidate_of extraction ──────────────────────────────────────────────────

def test_candidate_of_full_names():
    assert candidate_of(
        "Will Abelardo de la Espriella win the second round of the 2026 "
        "Colombian presidential election by 5-10%?") == "abelardo de la espriella"
    assert candidate_of(
        "Will Alex Bores be the democratic nominee for NY-12?") == "alex bores"


def test_candidate_of_strips_accents():
    assert candidate_of("Will José Antonio Kast win the election?") == "jose antonio kast"


def test_candidate_of_trump_excluded():
    assert candidate_of("Will Trump be president?") is None
    assert candidate_of("Will Donald Trump win the 2028 election?") is None


def test_candidate_of_generic_is_none():
    assert candidate_of("Will the Democrat win?") is None
    assert candidate_of("Will the next president be a Republican?") is None


def test_candidate_of_no_frame_is_none():
    assert candidate_of("Bitcoin up or down on June 21?") is None
    assert candidate_of("") is None
    assert candidate_of(None) is None


# ── guard: conflicts ─────────────────────────────────────────────────────────

def test_same_candidate_different_market_blocks():
    sess = _Sess([
        _incoming("Will Abelardo de la Espriella be president?"),
        _held([("Will Abelardo de la Espriella win the second round by 5-10%?", None, "M_HELD")]),
    ])
    res = _run(sess)
    assert res is not None
    cand, held_mid = res
    assert cand == "abelardo de la espriella"
    assert held_mid == "M_HELD"


def test_live_mode_same_candidate_blocks():
    sess = _Sess([
        _incoming("Will Abelardo de la Espriella be president?"),
        _Res(all_rows=[("Will Abelardo de la Espriella win by 5-10%?", None, "M_HELD")]),
    ])
    res = asyncio.run(risk_mod._politics_candidate_held(
        sess, mode="live", market_id="M_NEW"))
    assert res is not None and res[1] == "M_HELD"


# ── guard: allowed (no conflict) ─────────────────────────────────────────────

def test_different_candidate_allowed():
    sess = _Sess([
        _incoming("Will Abelardo de la Espriella be president?"),
        _held([("Will Alex Bores be the democratic nominee for NY-12?", None, "M_HELD")]),
    ])
    assert _run(sess) is None


def test_trump_incoming_exempt():
    # Trump short-circuits before the held query even runs.
    sess = _Sess([_incoming("Will Trump be president?")])
    assert _run(sess) is None


def test_non_politics_exempt():
    sess = _Sess([_incoming("Will Bitcoin be above $100k in 2026?", category="crypto")])
    assert _run(sess) is None


def test_unparseable_incoming_exempt():
    sess = _Sess([_incoming("Presidential election outcome 2026?")])
    assert _run(sess) is None


def test_same_market_id_skipped():
    sess = _Sess([
        _incoming("Will Abelardo de la Espriella be president?"),
        _held([("Will Abelardo de la Espriella be president?", None, "M_NEW")]),
    ])
    assert _run(sess, market_id="M_NEW") is None


# ── exclude-set sanity ───────────────────────────────────────────────────────

def test_exclude_set_is_trump_only():
    assert set(POLITICS_CANDIDATE_EXCLUDE) == {"trump"}
