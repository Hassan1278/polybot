"""Intra-Polymarket stat-arb scanner — PAPER-FIRST wiring around relations.py.

Pulls live state through polybot's existing infra (Market model + GammaClient +
ClobClient) and runs the pure no-arb core over it. This module is deliberately
**observe-only**: it finds and logs priced opportunities; it does NOT place
orders. Execution (lift every leg atomically via the executor's live path under
risk.preflight) is the next milestone and is intentionally absent here so the
edge can be validated against the live book first.

Two scans:

  * ``scan_binaries`` — every active binary market's YES+NO book, via
    ``binary_complement_arb``. Always structurally valid; needs no event
    metadata. This is the bread-and-butter scan.

  * ``scan_field`` — multi-outcome events, via ``field_buy_arb``. Gated on
    ``negRisk=true`` from Gamma (the venue's MECE guarantee) — we never field-arb
    a group we can't prove is mutually-exclusive-and-exhaustive, because that
    would manufacture a phantom edge.

In ``--loop`` mode a ``PersistenceTracker`` follows each opportunity across
passes and logs how long it survives + how its legs drift — the measurement that
tells us whether a two-leg lift is feasible before we wire any execution.

Run a one-shot paper scan from the repo root:

    python -m services.statarb.scanner               # binary + field, one pass
    python -m services.statarb.scanner --binary      # binary only
    python -m services.statarb.scanner --loop --interval 5   # persistence loop
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from polybot.clients import ClobClient, GammaClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market
from sqlalchemy import select

from services.statarb.persistence import PersistenceTracker
from services.statarb.relations import (
    ArbOpportunity,
    asks_from_book,
    best_ask,
    binary_complement_arb,
    field_buy_arb,
)

log = get_logger(__name__)


# ── scan configuration ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScanConfig:
    """Honest cost + gating knobs for a scan pass. Defaults mirror live
    Polymarket reality (0% CLOB taker fee, a few cents of redeem gas) and a
    deliberately conservative edge floor so dust violations don't register."""
    fee_bps: float = 0.0              # live CLOB taker fee (paper sim models 200; see relations.py)
    gas_usdc: float = 0.05            # on-chain cost to realize a complete set (redeem/merge)
    min_edge_usdc: float = 0.50       # ignore opportunities locking < this many dollars
    min_edge_bps: float = 30.0        # ...or returning < this ROI on locked capital
    min_liquidity_usdc: float = 200.0 # skip illiquid markets (their books are noise)
    max_markets: int = 600            # cap per pass (latency / rate-limit budget)
    book_chunk: int = 50              # tokens per /books POST (a ~1200-token batch 400s)
    book_fallback_cap: int = 300      # max singular /book GETs when the batch endpoint fails
    field_max_events: int = 30        # largest multi-outcome event_id groups to field-scan
    field_min_coverage: float = 0.98  # require we hold ~all of an event's outcomes (else Σ is a phantom)


# ── a priced opportunity + the market metadata to identify/log/track it ──────

@dataclass(frozen=True)
class ScanHit:
    """One priced ``ArbOpportunity`` plus the market context needed to log it
    and (later) execute it. Exposes the duck-typed surface the
    ``PersistenceTracker`` reads (``key``/``kind``/``net_usdc``/``edge_bps``/
    ``leg_px``) so it can be followed across passes by a stable identity."""
    opp: ArbOpportunity
    market_id: str
    slug: str
    question: str

    @property
    def key(self) -> str:
        """Stable identity of the bundle: its leg tokens, sorted. Immune to
        scan order; the same arb gets the same key every pass."""
        toks = sorted(str(lg.token_id) for lg in self.opp.legs if lg.token_id)
        return "|".join(toks) if toks else f"{self.opp.kind}:{self.market_id}"

    @property
    def kind(self) -> str:
        return self.opp.kind

    @property
    def net_usdc(self) -> float:
        return self.opp.net_usdc

    @property
    def edge_bps(self) -> float:
        return self.opp.edge_bps

    @property
    def leg_px(self) -> dict[str, float]:
        """Per-leg depth-weighted fill price, keyed by token id."""
        return {str(lg.token_id): lg.avg_price for lg in self.opp.legs if lg.token_id}


# ── candidate loading (reuses the Market model + Gamma) ──────────────────────

async def _active_binaries(s, cfg: ScanConfig) -> list[Market]:
    """Active, unresolved, two-token markets with enough liquidity to trust the
    book — the universe for the binary complementarity scan."""
    now = datetime.now(tz=timezone.utc)
    rows = (await s.execute(
        select(Market).where(
            Market.resolved.is_(False),
            Market.yes_token_id.is_not(None),
            Market.no_token_id.is_not(None),
            Market.liquidity_usdc >= cfg.min_liquidity_usdc,
            (Market.end_date.is_(None)) | (Market.end_date > now),
        ).order_by(Market.volume_24h_usdc.desc()).limit(cfg.max_markets)
    )).scalars().all()
    return list(rows)


def _book_index(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index a batch ``books()`` response by token id. Polymarket returns each
    book with its ``asset_id``; key on that so a dropped/empty book (CLOB omits
    tokens with no orderbook) doesn't misalign the rest by position."""
    out: dict[str, dict[str, Any]] = {}
    for b in results or []:
        tok = b.get("asset_id") or b.get("token_id") or b.get("asset")
        if tok:
            out[str(tok)] = b
    return out


async def _bounded_gather(factories: list, limit: int) -> list:
    """Run zero-arg async factories with bounded concurrency; a failing one
    yields None rather than aborting the batch."""
    sem = asyncio.Semaphore(max(1, limit))

    async def _run(f):
        async with sem:
            try:
                return await f()
            except Exception:  # noqa: BLE001
                return None

    return await asyncio.gather(*(_run(f) for f in factories))


async def _fetch_book_index(
    clob: ClobClient, tokens: list[str], cfg: ScanConfig, *, concurrency: int = 8
) -> dict[str, dict[str, Any]]:
    """Robustly fetch orderbooks for many tokens, keyed by token id.

    A single 1200-token POST to /books returns 400 (Polymarket caps the batch),
    so we validate + dedup the ids (they're uint256 decimal strings), fetch via
    /books in chunks, then fall back to the proven singular /book GET for any
    token the batch didn't return — covering both an oversized batch and an
    outright-broken batch endpoint. Resilient: a failed chunk degrades, it
    doesn't abort the pass. Emits a ``statarb_books`` line for visibility."""
    valid = [t for t in dict.fromkeys(str(x) for x in tokens) if t.isdigit()]
    if not valid:
        return {}

    chunks = [valid[i:i + cfg.book_chunk] for i in range(0, len(valid), cfg.book_chunk)]
    index: dict[str, dict[str, Any]] = {}
    for r in await _bounded_gather([lambda c=c: clob.books(c) for c in chunks], concurrency):
        if isinstance(r, list) and r:
            index.update(_book_index(r))
    via_batch = len(index)

    # Singular fallback ONLY when the batch returned nothing at all (the endpoint
    # is actually broken). When the batch works, tokens it omitted are genuinely
    # book-less — resolved/closed markets that 404 — and retrying them one-by-one
    # just floods 404s and burns a call per dead token every pass.
    if via_batch == 0:
        capped = valid[:cfg.book_fallback_cap]
        singles = await _bounded_gather([lambda t=t: clob.book(t) for t in capped], concurrency)
        for t, b in zip(capped, singles, strict=True):
            if isinstance(b, dict) and b:
                index[t] = b

    log.info("statarb_books", requested=len(valid), priced=len(index),
             via_batch=via_batch, via_fallback=len(index) - via_batch, chunk=cfg.book_chunk)
    return index


# ── binary complementarity scan ──────────────────────────────────────────────

async def scan_binaries(
    markets: list[Market], clob: ClobClient, cfg: ScanConfig
) -> list[ScanHit]:
    """Batch-fetch YES+NO books for each market and price the buy-both arb."""
    pairs = [(m, str(m.yes_token_id), str(m.no_token_id)) for m in markets]
    tokens = [t for _, y, n in pairs for t in (y, n)]
    if not tokens:
        return []

    by_token = await _fetch_book_index(clob, tokens, cfg)

    found: list[ScanHit] = []
    sums: list[tuple[float, str]] = []      # (best ask_yes + ask_no, market) per fully-priced market
    for m, yes_t, no_t in pairs:
        yb, nb = by_token.get(yes_t), by_token.get(no_t)
        if yb is None or nb is None:
            continue
        ya, na = asks_from_book(yb), asks_from_book(nb)
        by, bn = best_ask(ya), best_ask(na)
        if by is not None and bn is not None:
            sums.append((by + bn, m.slug or m.market_id))   # top-of-book complement cost
        opp = binary_complement_arb(
            ya, na,
            yes_token=yes_t,
            no_token=no_t,
            fee_bps=cfg.fee_bps,
            gas_usdc=cfg.gas_usdc,
            min_edge_usdc=cfg.min_edge_usdc,
            min_edge_bps=cfg.min_edge_bps,
        )
        if opp is not None:
            found.append(ScanHit(opp=opp, market_id=m.market_id, slug=m.slug, question=m.question))

    log.info("statarb_binary_universe", markets=len(pairs), priced_both=len(sums),
             opportunities=len(found))
    # Near-miss: how close does the book actually get to the $1 no-arb line? A
    # min_sum just above 1.0 means it grazes (fleeting violations plausible —
    # worth a streaming watchlist); a min_sum well above 1.0 is a structural wall.
    if sums:
        sums.sort(key=lambda x: x[0])
        min_sum, min_market = sums[0]
        log.info("statarb_near_miss", priced=len(sums), min_sum=round(min_sum, 4),
                 min_market=min_market[:48],
                 lt_1_00=sum(1 for s, _ in sums if s < 1.00),
                 lt_1_01=sum(1 for s, _ in sums if s < 1.01),
                 lt_1_02=sum(1 for s, _ in sums if s < 1.02),
                 lt_1_05=sum(1 for s, _ in sums if s < 1.05))
    return found


# ── multi-outcome "buy the field" scan (negRisk, grouped by event_id) ────────
#
# The events endpoint doesn't nest multi-outcome markets, so we reconstruct the
# groups from our OWN markets table via event_id and confirm each is a negRisk
# (MECE) event from the gamma market payload's ``negRisk`` flag (static -> cached).

_EVENT_META_CACHE: dict[str, tuple[bool, int]] = {}   # event_id -> (is_negRisk, true outcome count); static


async def _event_groups(s, cfg: ScanConfig) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """Multi-outcome groups from our markets table, keyed by event_id:
    ``[(event_id, [(market_id, yes_token, label), ...]), ...]``, biggest first,
    capped at ``field_max_events``. No per-market liquidity filter — a field needs
    ALL its legs, longshots included."""
    rows = (await s.execute(
        select(Market.event_id, Market.market_id, Market.yes_token_id, Market.question).where(
            Market.resolved.is_(False),
            Market.event_id.is_not(None),
            Market.yes_token_id.is_not(None),
        )
    )).all()
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for eid, mid, tok, q in rows:
        groups.setdefault(str(eid), []).append((mid, str(tok), (q or mid)[:48]))
    multi = [(eid, mem) for eid, mem in groups.items() if len(mem) >= 2]
    multi.sort(key=lambda x: len(x[1]), reverse=True)
    return multi[:cfg.field_max_events]


async def _event_meta(gamma: GammaClient, event_id: str) -> tuple[bool, int]:
    """``(is_negRisk, true_outcome_count)`` for an event from its gamma payload,
    cached (static). negRisk = MECE (exactly one YES wins); the true count is how
    many outcomes the event REALLY has, so we can tell a complete field from a
    longshot subset of one (e.g. holding 37 of a 128-candidate election)."""
    if event_id not in _EVENT_META_CACHE:
        try:
            ev = await gamma.get(f"/events/{event_id}") or {}
        except Exception:  # noqa: BLE001
            ev = {}
        mks = ev.get("markets") or []
        is_neg = bool(ev.get("enableNegRisk") or (mks and mks[0].get("negRisk")))
        _EVENT_META_CACHE[event_id] = (is_neg, len(mks))
    return _EVENT_META_CACHE[event_id]


async def scan_field(gamma: GammaClient, clob: ClobClient, cfg: ScanConfig) -> list[ScanHit]:
    """Scan multi-outcome **negRisk** events (grouped from our markets table by
    event_id) for a buy-the-field violation: Σ best-YES-asks across ALL outcomes
    < $1. Only negRisk groups are MECE, so only those are field-arbable.

    Completeness is enforced: "buy the field" guarantees $1 only if we hold EVERY
    outcome that can still win. Our markets table mirrors just a subset of big
    events (e.g. 37 of a 128-candidate election — all longshots), and a Σ<1 over a
    subset is a PHANTOM (the winner may be an outcome we don't hold). So we require
    near-full coverage of the event's true outcome count before trusting the sum."""
    async with session_scope() as s:
        groups = await _event_groups(s, cfg)

    found: list[ScanHit] = []
    n_negrisk = n_undersized = n_incomplete = 0
    sums: list[tuple[float, str, int]] = []     # (Σ best YES asks, event_id, n_legs)

    for eid, members in groups:
        is_neg, true_count = await _event_meta(gamma, eid)
        if not is_neg:
            continue                                   # not MECE -> never field-arb
        n_negrisk += 1
        coverage = (len(members) / true_count) if true_count else 0.0
        if coverage < cfg.field_min_coverage:
            n_undersized += 1                          # we hold only a subset -> Σ is a phantom
            continue
        by_token = await _fetch_book_index(clob, [tok for _, tok, _ in members], cfg)
        outcome_asks, labels, token_ids = [], [], []
        ok = True
        for _mid, tok, label in members:
            book = by_token.get(tok)
            if book is None:
                ok = False
                break
            outcome_asks.append(asks_from_book(book))
            labels.append(label)
            token_ids.append(tok)
        if not ok:
            n_incomplete += 1                          # a missing leg breaks the $1 guarantee
            continue

        bests = [best_ask(a) for a in outcome_asks]
        if all(b is not None for b in bests):
            sums.append((sum(bests), eid, len(bests)))

        opp = field_buy_arb(
            outcome_asks, labels=labels, token_ids=token_ids,
            fee_bps=cfg.fee_bps, gas_usdc=cfg.gas_usdc,
            min_edge_usdc=cfg.min_edge_usdc, min_edge_bps=cfg.min_edge_bps,
        )
        if opp is not None:
            found.append(ScanHit(opp=opp, market_id=eid, slug=eid, question=labels[0]))

    # Funnel: groups -> negRisk -> (dropped: undersized / incomplete) -> evaluable -> opps.
    log.info("statarb_field_universe", groups=len(groups), negrisk=n_negrisk,
             undersized=n_undersized, incomplete=n_incomplete, evaluable=len(sums),
             opportunities=len(found), cached=len(_EVENT_META_CACHE))
    if sums:
        sums.sort(key=lambda x: x[0])
        min_sum, min_event, min_legs = sums[0]
        log.info("statarb_field_near_miss", evaluable=len(sums), min_sum=round(min_sum, 4),
                 min_event=min_event, min_legs=min_legs,
                 lt_1_00=sum(1 for s, _, _ in sums if s < 1.00),
                 lt_1_01=sum(1 for s, _, _ in sums if s < 1.01),
                 lt_1_02=sum(1 for s, _, _ in sums if s < 1.02),
                 lt_1_05=sum(1 for s, _, _ in sums if s < 1.05))
    return found


# ── logging ──────────────────────────────────────────────────────────────────

def _log_opportunity(hit: ScanHit) -> None:
    """Emit one structured ``statarb_opportunity`` line (paper — no order placed)."""
    opp = hit.opp
    log.info(
        "statarb_opportunity",
        kind=opp.kind,
        market=hit.market_id,
        slug=hit.slug,
        question=hit.question[:80],
        legs=[{"label": lg.label, "px": round(lg.avg_price, 4), "tok": lg.token_id} for lg in opp.legs],
        shares=round(opp.shares, 2),
        cost_usdc=round(opp.cost_usdc, 2),
        payout_usdc=round(opp.payout_usdc, 2),
        net_usdc=round(opp.net_usdc, 2),
        edge_bps=round(opp.edge_bps, 1),
        paper=True,
    )


# ── orchestration ────────────────────────────────────────────────────────────

async def scan_once(
    cfg: ScanConfig | None = None, *, do_field: bool = True, log_each: bool = True
) -> list[ScanHit]:
    """One paper scan pass (binary + optionally field). Returns every opportunity
    found. Logs each when ``log_each`` (single-shot CLI); the loop suppresses
    per-pass spam and lets the persistence tracker do the logging instead. Never
    places an order."""
    cfg = cfg or ScanConfig()
    clob, gamma = ClobClient(), GammaClient()
    try:
        async with session_scope() as s:
            markets = await _active_binaries(s, cfg)
        log.info("statarb_scan_start", binaries=len(markets), field=do_field,
                 fee_bps=cfg.fee_bps, min_edge_bps=cfg.min_edge_bps)
        hits = await scan_binaries(markets, clob, cfg)
        if do_field:
            hits += await scan_field(gamma, clob, cfg)
        if log_each:
            for h in hits:
                _log_opportunity(h)
        log.info("statarb_scan_done", opportunities=len(hits),
                 net_usdc=round(sum(h.net_usdc for h in hits), 2))
        return hits
    finally:
        await clob.close()
        await gamma.close()


async def scan_loop(
    cfg: ScanConfig | None = None, *, interval_seconds: int = 30, summary_every: int = 20
) -> None:
    """Periodic paper scan with persistence tracking. Resilient (catch/log/sleep).
    NOT wired into the executor — run standalone until the edge is validated.

    Each pass: snapshot opportunities, fold them into the tracker, and log the
    first sighting of new ones + the full lifetime/decay of any that just
    expired. Every ``summary_every`` passes, log the rollup (median lifespan,
    fraction that survived a legging window, fraction that lasted one pass)."""
    log.info("statarb_loop_starting", interval=interval_seconds)
    tracker = PersistenceTracker()
    passes = 0
    while True:
        try:
            hits = await scan_once(cfg, log_each=False)
            new, expired = tracker.update(hits, time.monotonic())

            for t in new:
                log.info("statarb_opportunity_new", kind=t.kind, slug=t.slug, market=t.market_id,
                         net_usdc=round(t.net_first, 2), edge_bps=round(t.edge_bps_first, 1))
            for t in expired:
                log.info("statarb_opportunity_expired", kind=t.kind, slug=t.slug, market=t.market_id,
                         lifetime_s=round(t.lifetime_s, 1), observations=t.observations,
                         net_first=round(t.net_first, 2), net_last=round(t.net_last, 2),
                         net_min=round(t.net_min, 2),
                         max_adverse_drift=round(t.max_adverse_drift, 4),
                         leg_drift={k: round(v, 4) for k, v in t.leg_drift().items()})

            passes += 1
            if summary_every and passes % summary_every == 0:
                log.info("statarb_persistence_summary", live=len(tracker.live), **tracker.summary())
        except Exception:  # noqa: BLE001
            log.exception("statarb_scan_failed")
        await asyncio.sleep(max(2, interval_seconds))


def _main() -> None:
    # The CLOB read path logs a line per HTTP request; for a 600-market sweep
    # that buries our structured output. Quiet the transport loggers.
    for _name in ("httpx", "httpcore"):
        logging.getLogger(_name).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Intra-Polymarket stat-arb paper scan")
    ap.add_argument("--binary", action="store_true", help="binary scan only (skip field)")
    ap.add_argument("--loop", action="store_true", help="scan continuously with persistence tracking")
    ap.add_argument("--interval", type=int, default=30, help="seconds between passes in --loop")
    args = ap.parse_args()
    cfg = ScanConfig()
    if args.loop:
        asyncio.run(scan_loop(cfg, interval_seconds=args.interval))
    else:
        found = asyncio.run(scan_once(cfg, do_field=not args.binary))
        print(f"\n{len(found)} opportunity(ies); "
              f"${sum(h.net_usdc for h in found):.2f} net locked (paper).")


if __name__ == "__main__":
    _main()
