"""Tests for the stat-arb persistence tracker (services/statarb/persistence.py).

The tracker is pure + clock-injected: update(hits, now) is deterministic given
synthetic timestamps, so lifespans, edge decay, and per-leg drift are all exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.statarb.persistence import PersistenceTracker, Tracked


@dataclass
class FakeHit:
    """Minimal stand-in for scanner.ScanHit (the attributes update() reads)."""
    key: str
    kind: str = "binary_complement"
    slug: str = "slug"
    market_id: str = "mkt"
    net_usdc: float = 10.0
    edge_bps: float = 100.0
    leg_px: dict = field(default_factory=dict)


# ── appearance / disappearance ───────────────────────────────────────────────

def test_new_opportunity_is_reported_once():
    tr = PersistenceTracker()
    new, expired = tr.update([FakeHit(key="A")], now=0.0)
    assert [t.key for t in new] == ["A"]
    assert expired == []
    assert len(tr.live) == 1
    # Seen again next pass -> neither new nor expired, just updated.
    new, expired = tr.update([FakeHit(key="A")], now=10.0)
    assert new == [] and expired == []
    assert tr.live[0].observations == 2


def test_expiry_when_absent_carries_lifetime():
    tr = PersistenceTracker()
    tr.update([FakeHit(key="A")], now=0.0)
    tr.update([FakeHit(key="A")], now=10.0)
    new, expired = tr.update([], now=20.0)            # gone this pass
    assert new == []
    assert len(expired) == 1
    t = expired[0]
    assert t.key == "A"
    assert t.observations == 2                         # seen at t=0 and t=10
    assert t.lifetime_s == 10.0                        # first->last sighting
    assert tr.live == []                               # removed from live set


def test_single_pass_opportunity_has_zero_lifetime():
    tr = PersistenceTracker()
    tr.update([FakeHit(key="A")], now=5.0)
    _, expired = tr.update([], now=6.0)
    assert expired[0].lifetime_s == 0.0                # seen exactly once
    assert expired[0].observations == 1


# ── edge decay over life ─────────────────────────────────────────────────────

def test_net_first_last_min_max_tracked():
    tr = PersistenceTracker()
    tr.update([FakeHit(key="A", net_usdc=10.0)], now=0.0)
    tr.update([FakeHit(key="A", net_usdc=6.0)], now=10.0)    # dipped
    tr.update([FakeHit(key="A", net_usdc=8.0)], now=20.0)    # partial recover
    _, expired = tr.update([], now=30.0)
    t = expired[0]
    assert t.net_first == 10.0
    assert t.net_last == 8.0
    assert t.net_min == 6.0
    assert t.net_max == 10.0
    assert t.net_decay == 2.0                          # first - last


# ── per-leg price drift ──────────────────────────────────────────────────────

def test_leg_drift_signed_first_to_last():
    tr = PersistenceTracker()
    tr.update([FakeHit(key="A", leg_px={"Y": 0.40, "N": 0.45})], now=0.0)
    tr.update([FakeHit(key="A", leg_px={"Y": 0.45, "N": 0.44})], now=10.0)
    _, expired = tr.update([], now=20.0)
    drift = expired[0].leg_drift()
    assert abs(drift["Y"] - 0.05) < 1e-9               # ask rose 5c (worse to buy)
    assert abs(drift["N"] - (-0.01)) < 1e-9            # ask fell 1c


def test_max_adverse_drift_catches_spike_then_recovery():
    # The leg spiked to 0.50 mid-life then settled at 0.42; the worst case you'd
    # have chased was +0.10, even though the first->last drift is only +0.02.
    tr = PersistenceTracker()
    tr.update([FakeHit(key="A", leg_px={"Y": 0.40})], now=0.0)
    tr.update([FakeHit(key="A", leg_px={"Y": 0.50})], now=10.0)
    tr.update([FakeHit(key="A", leg_px={"Y": 0.42})], now=20.0)
    _, expired = tr.update([], now=30.0)
    t = expired[0]
    assert abs(t.max_adverse_drift - 0.10) < 1e-9
    assert abs(t.leg_drift()["Y"] - 0.02) < 1e-9


# ── multiple concurrent opportunities ────────────────────────────────────────

def test_independent_keys_tracked_separately():
    tr = PersistenceTracker()
    tr.update([FakeHit(key="A"), FakeHit(key="B")], now=0.0)
    # A persists, B vanishes, C appears.
    new, expired = tr.update([FakeHit(key="A"), FakeHit(key="C")], now=10.0)
    assert {t.key for t in new} == {"C"}
    assert {t.key for t in expired} == {"B"}
    assert {t.key for t in tr.live} == {"A", "C"}


# ── rollup summary ───────────────────────────────────────────────────────────

def test_summary_aggregates_expired_history():
    tr = PersistenceTracker()
    # One long-lived (20s, 3 obs) and one fleeting (single pass) opportunity.
    tr.update([FakeHit(key="long", net_usdc=10.0)], now=0.0)
    tr.update([FakeHit(key="long", net_usdc=7.0)], now=10.0)
    tr.update([FakeHit(key="long", net_usdc=7.0), FakeHit(key="blip")], now=20.0)
    tr.update([], now=30.0)                             # both expire here
    s = tr.summary(survive_threshold_s=5.0)
    assert s["expired"] == 2
    assert s["max_lifetime_s"] == 20.0
    assert s["frac_survive_threshold"] == 0.5          # only "long" cleared 5s
    assert s["frac_single_pass"] == 0.5                # only "blip" was single-pass
    assert s["median_net_decay_usdc"] == 1.5           # decays: long=3.0, blip=0.0 -> median 1.5


def test_summary_empty_history():
    assert PersistenceTracker().summary() == {"expired": 0}


def test_tracked_is_the_returned_object():
    # Sanity: expired returns the actual Tracked instances (not copies).
    tr = PersistenceTracker()
    tr.update([FakeHit(key="A")], now=0.0)
    _, expired = tr.update([], now=1.0)
    assert isinstance(expired[0], Tracked)
