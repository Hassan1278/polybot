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

Run a one-shot paper scan from the repo root:

    python -m services.statarb.scanner            # binary + field
    python -m services.statarb.scanner --binary   # binary only
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from polybot.clients import ClobClient, GammaClient
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Market
from sqlalchemy import select

from services.statarb.relations import (
    ArbOpportunity,
    asks_from_book,
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


# ── binary complementarity scan ──────────────────────────────────────────────

async def scan_binaries(
    markets: list[Market], clob: ClobClient, cfg: ScanConfig
) -> list[ArbOpportunity]:
    """Batch-fetch YES+NO books for each market and price the buy-both arb."""
    pairs = [(m, str(m.yes_token_id), str(m.no_token_id)) for m in markets]
    tokens = [t for _, y, n in pairs for t in (y, n)]
    if not tokens:
        return []

    by_token = _book_index(await clob.books(tokens))

    found: list[ArbOpportunity] = []
    for m, yes_t, no_t in pairs:
        yb, nb = by_token.get(yes_t), by_token.get(no_t)
        if yb is None or nb is None:
            continue
        opp = binary_complement_arb(
            asks_from_book(yb),
            asks_from_book(nb),
            yes_token=yes_t,
            no_token=no_t,
            fee_bps=cfg.fee_bps,
            gas_usdc=cfg.gas_usdc,
            min_edge_usdc=cfg.min_edge_usdc,
            min_edge_bps=cfg.min_edge_bps,
        )
        if opp is not None:
            _log_opportunity(opp, market_id=m.market_id, slug=m.slug, question=m.question)
            found.append(opp)
    return found


# ── multi-outcome "buy the field" scan (negRisk-gated) ───────────────────────

def _yes_token_of(gamma_market: dict[str, Any]) -> str | None:
    """The YES (outcomes[0] ↔ clobTokenIds[0]) token of a Gamma market payload.
    For a negRisk event each child market is one outcome; its YES = that outcome
    winning."""
    toks = gamma_market.get("clobTokenIds") or gamma_market.get("clob_token_ids")
    if isinstance(toks, str):
        import json
        try:
            toks = json.loads(toks)
        except (ValueError, TypeError):
            toks = None
    if isinstance(toks, list) and toks:
        return str(toks[0])
    return None


async def scan_field(
    gamma: GammaClient, clob: ClobClient, cfg: ScanConfig, *, event_limit: int = 200
) -> list[ArbOpportunity]:
    """Scan active **negRisk** events for a buy-the-field violation. We only ever
    field-arb a group Gamma marks ``negRisk=true`` — that's the venue promising
    the outcomes are mutually-exclusive-and-exhaustive (exactly one YES wins)."""
    events = await gamma.events(limit=event_limit, active=True)
    found: list[ArbOpportunity] = []

    for ev in events or []:
        if not ev.get("negRisk"):
            continue                                   # MECE not guaranteed -> never field-arb
        mkts = [m for m in (ev.get("markets") or []) if not m.get("closed")]
        legs = [(m, _yes_token_of(m)) for m in mkts]
        legs = [(m, t) for m, t in legs if t]
        if len(legs) < 2:
            continue

        by_token = _book_index(await clob.books([t for _, t in legs]))
        outcome_asks, labels, token_ids = [], [], []
        ok = True
        for m, tok in legs:
            book = by_token.get(tok)
            if book is None:
                ok = False
                break
            outcome_asks.append(asks_from_book(book))
            labels.append(str(m.get("groupItemTitle") or m.get("question") or tok)[:48])
            token_ids.append(tok)
        if not ok:
            continue                                   # a missing leg breaks the guarantee

        opp = field_buy_arb(
            outcome_asks,
            labels=labels,
            token_ids=token_ids,
            fee_bps=cfg.fee_bps,
            gas_usdc=cfg.gas_usdc,
            min_edge_usdc=cfg.min_edge_usdc,
            min_edge_bps=cfg.min_edge_bps,
        )
        if opp is not None:
            _log_opportunity(
                opp, market_id=str(ev.get("id") or ""), slug=str(ev.get("slug") or ""),
                question=str(ev.get("title") or ""),
            )
            found.append(opp)
    return found


# ── logging ──────────────────────────────────────────────────────────────────

def _log_opportunity(opp: ArbOpportunity, *, market_id: str, slug: str, question: str) -> None:
    """Emit one structured ``statarb_opportunity`` line (paper — no order placed)."""
    log.info(
        "statarb_opportunity",
        kind=opp.kind,
        market=market_id,
        slug=slug,
        question=question[:80],
        legs=[{"label": lg.label, "px": round(lg.avg_price, 4), "tok": lg.token_id} for lg in opp.legs],
        shares=round(opp.shares, 2),
        cost_usdc=round(opp.cost_usdc, 2),
        payout_usdc=round(opp.payout_usdc, 2),
        net_usdc=round(opp.net_usdc, 2),
        edge_bps=round(opp.edge_bps, 1),
        paper=True,
    )


# ── orchestration ────────────────────────────────────────────────────────────

async def scan_once(cfg: ScanConfig | None = None, *, do_field: bool = True) -> list[ArbOpportunity]:
    """One paper scan pass (binary + optionally field). Returns every opportunity
    found; logs each. Never places an order."""
    cfg = cfg or ScanConfig()
    clob, gamma = ClobClient(), GammaClient()
    try:
        async with session_scope() as s:
            markets = await _active_binaries(s, cfg)
        log.info("statarb_scan_start", binaries=len(markets), field=do_field,
                 fee_bps=cfg.fee_bps, min_edge_bps=cfg.min_edge_bps)
        found = await scan_binaries(markets, clob, cfg)
        if do_field:
            found += await scan_field(gamma, clob, cfg)
        log.info("statarb_scan_done", opportunities=len(found),
                 net_usdc=round(sum(o.net_usdc for o in found), 2))
        return found
    finally:
        await clob.close()
        await gamma.close()


async def scan_loop(cfg: ScanConfig | None = None, *, interval_seconds: int = 30) -> None:
    """Periodic paper scan. Resilient (catch/log/sleep). NOT wired into the
    executor yet — run standalone until the edge is validated, then graduate."""
    log.info("statarb_loop_starting", interval=interval_seconds)
    while True:
        try:
            await scan_once(cfg)
        except Exception:  # noqa: BLE001
            log.exception("statarb_scan_failed")
        await asyncio.sleep(max(5, interval_seconds))


def _main() -> None:
    ap = argparse.ArgumentParser(description="Intra-Polymarket stat-arb paper scan")
    ap.add_argument("--binary", action="store_true", help="binary scan only (skip field)")
    ap.add_argument("--loop", action="store_true", help="scan continuously")
    args = ap.parse_args()
    cfg = ScanConfig()
    if args.loop:
        asyncio.run(scan_loop(cfg))
    else:
        found = asyncio.run(scan_once(cfg, do_field=not args.binary))
        print(f"\n{len(found)} opportunity(ies); "
              f"${sum(o.net_usdc for o in found):.2f} net locked (paper).")


if __name__ == "__main__":
    _main()
