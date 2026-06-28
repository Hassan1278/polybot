"""Persistence tracking for stat-arb opportunities — does an edge survive long
enough to actually leg into it?

The scanner takes a snapshot of live opportunities each pass. This module diffs
consecutive snapshots to measure, per opportunity:

  * how long it lived (observed lifespan) and how many passes we saw it in,
  * whether the locked edge held or decayed (net first / last / min over life),
  * how each leg's fill price drifted while it was alive (the leg you'd be
    chasing to complete the bundle).

Those are the numbers that decide whether the two-leg lift is feasible — and,
downstream, whether a low-latency (Rust) hot path would ever matter. If edges
routinely survive many seconds, you're not in a latency race; if they vanish
within a single pass, you are.

Design: PURE + clock-injected. ``update(hits, now)`` is deterministic given its
inputs and prior state — no wall-clock read inside — so it's unit-tested with
synthetic timestamps. The scanner feeds it ``time.monotonic()`` and logs what it
returns.

Granularity caveat: lifespans are measured at the SCAN INTERVAL. A 5–10s loop
cleanly separates "gone within one pass" from "survives minutes" — enough to
answer the strategic question. Measuring the sub-second legging window itself
needs a narrow, fast-polled watchlist (a follow-up), not a 600-market sweep.

``update`` accepts any object exposing: ``key`` (stable identity), ``kind``,
``slug``, ``market_id``, ``net_usdc``, ``edge_bps``, and ``leg_px`` (a
``{token_id: fill_price}`` dict). ``scanner.ScanHit`` satisfies this; tests use a
small fake.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


def _median(xs: Sequence[float]) -> float:
    n = len(xs)
    if n == 0:
        return 0.0
    s = sorted(xs)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


@dataclass
class Tracked:
    """The accumulated history of one opportunity across the passes it appeared
    in. Times are monotonic seconds (whatever clock the caller injected)."""
    key: str
    kind: str
    slug: str
    market_id: str
    first_seen: float
    last_seen: float
    observations: int
    net_first: float
    net_last: float
    net_min: float
    net_max: float
    edge_bps_first: float
    edge_bps_last: float
    px_first: dict[str, float]      # per-leg fill price at first sighting
    px_last: dict[str, float]       # ...at last sighting
    px_max: dict[str, float]        # highest (worst-to-buy) fill price ever seen per leg

    @property
    def lifetime_s(self) -> float:
        """Observed lifespan: first → last sighting. 0.0 means seen in a single
        pass (so it lived less than one scan interval)."""
        return max(0.0, self.last_seen - self.first_seen)

    @property
    def net_decay(self) -> float:
        """How much the locked edge fell from first to last sighting (>0 = it
        decayed; <0 = it actually got better)."""
        return self.net_first - self.net_last

    def leg_drift(self) -> dict[str, float]:
        """Signed per-leg fill-price drift, first → last. >0 = the ask rose =
        more expensive to complete that leg later."""
        return {t: self.px_last[t] - self.px_first.get(t, self.px_last[t]) for t in self.px_last}

    @property
    def max_adverse_drift(self) -> float:
        """Largest UPWARD fill-price move on any single leg over the whole life —
        the worst case you'd have chased to finish the bundle, even if it later
        recovered."""
        drifts = [self.px_max[t] - self.px_first.get(t, self.px_max[t]) for t in self.px_max]
        return max(drifts) if drifts else 0.0


class PersistenceTracker:
    """Stateful across passes. Hold one instance for the life of a scan loop and
    call ``update`` once per pass with the current opportunities."""

    def __init__(self, *, history_cap: int = 2000) -> None:
        self._live: dict[str, Tracked] = {}
        self._history: list[Tracked] = []     # expired records, bounded
        self._cap = history_cap

    def update(self, hits: Sequence[Any], now: float) -> tuple[list[Tracked], list[Tracked]]:
        """Fold this pass's opportunities into the tracked state.

        Returns ``(new, expired)``: ``new`` = keys seen for the first time;
        ``expired`` = keys that were live but are absent this pass (their
        ``Tracked`` carries the full lifetime stats — the money metric). Keys
        present again are updated in place and returned in neither list."""
        seen: set[str] = set()
        new: list[Tracked] = []

        for h in hits:
            seen.add(h.key)
            px = dict(h.leg_px)
            t = self._live.get(h.key)
            if t is None:
                self._live[h.key] = t = Tracked(
                    key=h.key, kind=h.kind, slug=h.slug, market_id=h.market_id,
                    first_seen=now, last_seen=now, observations=1,
                    net_first=h.net_usdc, net_last=h.net_usdc,
                    net_min=h.net_usdc, net_max=h.net_usdc,
                    edge_bps_first=h.edge_bps, edge_bps_last=h.edge_bps,
                    px_first=px, px_last=dict(px), px_max=dict(px),
                )
                new.append(t)
            else:
                t.last_seen = now
                t.observations += 1
                t.net_last = h.net_usdc
                t.net_min = min(t.net_min, h.net_usdc)
                t.net_max = max(t.net_max, h.net_usdc)
                t.edge_bps_last = h.edge_bps
                t.px_last = px
                for tok, p in px.items():
                    t.px_max[tok] = max(t.px_max.get(tok, p), p)

        expired = [t for k, t in self._live.items() if k not in seen]
        for t in expired:
            del self._live[t.key]
        self._history.extend(expired)
        if len(self._history) > self._cap:
            self._history = self._history[-self._cap:]
        return new, expired

    @property
    def live(self) -> list[Tracked]:
        return list(self._live.values())

    def summary(self, *, survive_threshold_s: float = 5.0) -> dict[str, Any]:
        """Roll up every expired opportunity so far into the decision metrics:
        median/max lifespan, the fraction that survived past a legging-window
        threshold, the fraction that lasted only a single pass (fleeting), and
        the median edge decay over life."""
        hist = self._history
        n = len(hist)
        if n == 0:
            return {"expired": 0}
        lifes = [t.lifetime_s for t in hist]
        survived = sum(1 for t in hist if t.lifetime_s >= survive_threshold_s)
        single = sum(1 for t in hist if t.observations <= 1)
        return {
            "expired": n,
            "median_lifetime_s": round(_median(lifes), 1),
            "max_lifetime_s": round(max(lifes), 1),
            "survive_threshold_s": survive_threshold_s,
            "frac_survive_threshold": round(survived / n, 3),
            "frac_single_pass": round(single / n, 3),
            "median_net_decay_usdc": round(_median([t.net_decay for t in hist]), 3),
        }
