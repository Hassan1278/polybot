"""Intra-Polymarket no-arbitrage core — PURE, I/O-free, fully unit-tested.

This is the heart of the stat-arb strategy. It encodes the two *structural*
price relationships that MUST hold on a single venue, and computes the exact,
depth-aware, fee-net profit of buying through a violation. No network, no DB,
no clock — just orderbook ladders in, an ``ArbOpportunity`` (or ``None``) out.

Two relationships (both: "buy a basket that is guaranteed to pay $1"):

  1. Binary YES/NO complementarity (``binary_complement_arb``)
     A binary market's YES and NO tokens are complementary: at resolution
     exactly one pays $1. So 1 YES + 1 NO is a guaranteed $1. If you can lift
     both asks for less than $1 (net of fees), the difference is locked profit.
     Always valid for any binary market — no semantic assumptions.

  2. Multi-outcome "buy the field" / negative-risk (``field_buy_arb``)
     A mutually-exclusive, collectively-exhaustive (MECE) event with K outcomes
     resolves to exactly ONE winning YES. So 1 YES of every outcome is a
     guaranteed $1. If Σ asks across the K outcomes < $1 (net of fees), that's
     locked profit. CORRECTNESS PRECONDITION: the caller MUST only pass a set of
     outcomes that is genuinely MECE — on Polymarket that means an event flagged
     ``negRisk=true``. Passing a non-exhaustive group (e.g. unrelated "will X
     happen by date" markets) produces a FALSE arb. The scanner enforces this.

Both reduce to the same math: walk K ask ladders in lockstep, accumulate
"baskets" (one share of each leg) while the marginal basket costs less than the
$1 payout net of the proportional fee, then gate the total on fixed costs (gas)
and a minimum edge. Walking deeper raises each leg's ask, so there is a natural
optimal size where the marginal basket's edge hits zero — we stop there.

Fee model (honest + tunable): ``fee_bps`` is a proportional taker fee charged on
spend, ``gas_usdc`` a fixed on-chain cost to realize the basket (redeem/merge).
Polymarket's live CLOB taker fee is currently 0%; the paper simulator models a
conservative 2% (``FEE_BPS=200``). Defaults here mirror live reality (0 bps); the
scanner passes whatever the operator configures so edge is never overstated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

_EPS = 1e-9


# ── result types ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ArbLeg:
    """One side of an arb basket: how much to buy of which token, at what
    depth-weighted average price."""
    label: str                 # "YES" / "NO" / outcome name
    token_id: str | None       # CLOB token to lift (None in pure tests)
    avg_price: float           # VWAP across the levels consumed for this leg
    shares: float              # share count (equal across all legs of a basket)


@dataclass(frozen=True)
class ArbOpportunity:
    """A priced, depth-aware, fee-net arbitrage. ``net_usdc`` is the locked
    profit after lifting every leg's asks and paying fees; it is realized at
    resolution (hold the complete set) or immediately (merge/redeem on-chain)."""
    kind: str                  # "binary_complement" | "field_buy"
    shares: float              # baskets bought (== shares per leg)
    cost_usdc: float           # total spent across all legs
    payout_usdc: float         # guaranteed return (= shares × $1 payout)
    fees_usdc: float           # proportional fee on spend + fixed gas
    net_usdc: float            # payout − cost − fees  (the edge, in dollars)
    edge_bps: float            # net / cost, in basis points (ROI on locked capital)
    legs: tuple[ArbLeg, ...]

    @property
    def n_legs(self) -> int:
        return len(self.legs)


# ── orderbook helpers (pure) ─────────────────────────────────────────────────

def asks_from_book(book: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The ask levels of a CLOB ``/book`` payload (``[]`` if none). BUY lifts asks."""
    return list((book or {}).get("asks") or [])


def bids_from_book(book: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The bid levels of a CLOB ``/book`` payload (``[]`` if none)."""
    return list((book or {}).get("bids") or [])


def _ladder(levels: Sequence[dict[str, Any]] | None) -> list[tuple[float, float]]:
    """Normalize raw CLOB levels ``[{"price","size"}, ...]`` (string fields) into
    a price-ascending list of ``(price, size)`` floats. Drops malformed /
    non-positive levels. Ascending = cheapest first, the order you'd lift asks."""
    out: list[tuple[float, float]] = []
    for lv in levels or []:
        try:
            p = float(lv["price"])
            s = float(lv["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if p > 0.0 and s > 0.0:
            out.append((p, s))
    out.sort(key=lambda x: x[0])
    return out


def _label(labels: Sequence[str] | None, k: int) -> str:
    if labels and k < len(labels) and labels[k]:
        return str(labels[k])
    return f"leg{k}"


def _at(seq: Sequence[Any] | None, k: int) -> Any:
    return seq[k] if seq and k < len(seq) else None


# ── the shared no-arb walk ───────────────────────────────────────────────────

def _field_walk(
    ladders: list[list[tuple[float, float]]], payout: float, fee_frac: float
) -> tuple[float, list[float]]:
    """Lockstep K-way walk. Accumulate baskets (one share of every leg) while the
    marginal basket's gross cost, grossed up by the proportional fee, stays below
    the ``payout``. Returns ``(total_baskets, per_leg_spend)``.

    Greedy is optimal here: marginal basket cost is non-decreasing as we consume
    cheaper levels first, so once a basket stops being profitable, every later one
    is too — we stop at the exact point marginal edge hits zero."""
    k = len(ladders)
    idx = [0] * k
    rem = [ladders[i][0][1] for i in range(k)]      # size left at each leg's current level
    baskets = 0.0
    leg_spend = [0.0] * k

    while all(idx[i] < len(ladders[i]) for i in range(k)):
        prices = [ladders[i][idx[i]][0] for i in range(k)]
        marginal = sum(prices)
        if marginal * (1.0 + fee_frac) >= payout:
            break                                    # next basket is not (net) profitable
        take = min(rem)
        if take <= _EPS:
            break
        baskets += take
        for i in range(k):
            leg_spend[i] += prices[i] * take
            rem[i] -= take
            if rem[i] <= _EPS:                       # exhausted this level → advance
                idx[i] += 1
                rem[i] = ladders[i][idx[i]][1] if idx[i] < len(ladders[i]) else 0.0
    return baskets, leg_spend


def _arb(
    leg_asks: Sequence[Sequence[dict[str, Any]]],
    *,
    labels: Sequence[str] | None,
    token_ids: Sequence[str] | None,
    kind: str,
    payout: float,
    fee_bps: float,
    gas_usdc: float,
    min_edge_usdc: float,
    min_edge_bps: float,
) -> ArbOpportunity | None:
    """Core: price the cheapest profitable basket across ``leg_asks`` ladders.
    Returns None when there's no (sufficient) violation. Shared by both public
    entry points — binary is just the K=2 case."""
    ladders = [_ladder(a) for a in leg_asks]
    if len(ladders) < 2 or any(not lad for lad in ladders):
        return None

    fee_frac = max(0.0, fee_bps) / 10_000.0
    baskets, leg_spend = _field_walk(ladders, payout, fee_frac)
    if baskets <= _EPS:
        return None

    cost = sum(leg_spend)
    payout_total = payout * baskets
    fees = cost * fee_frac + max(0.0, gas_usdc)
    net = payout_total - cost - fees
    edge_bps = (net / cost) * 10_000.0 if cost > 0.0 else 0.0

    if net < min_edge_usdc or edge_bps < min_edge_bps:
        return None

    legs = tuple(
        ArbLeg(
            label=_label(labels, i),
            token_id=_at(token_ids, i),
            avg_price=leg_spend[i] / baskets,
            shares=baskets,
        )
        for i in range(len(ladders))
    )
    return ArbOpportunity(
        kind=kind,
        shares=baskets,
        cost_usdc=cost,
        payout_usdc=payout_total,
        fees_usdc=fees,
        net_usdc=net,
        edge_bps=edge_bps,
        legs=legs,
    )


# ── public entry points ──────────────────────────────────────────────────────

def binary_complement_arb(
    yes_asks: Sequence[dict[str, Any]],
    no_asks: Sequence[dict[str, Any]],
    *,
    yes_token: str | None = None,
    no_token: str | None = None,
    payout: float = 1.0,
    fee_bps: float = 0.0,
    gas_usdc: float = 0.0,
    min_edge_usdc: float = 0.0,
    min_edge_bps: float = 0.0,
) -> ArbOpportunity | None:
    """Buy-both arb on ONE binary market: lift YES and NO asks together for less
    than the $1 they're jointly guaranteed to pay. Always structurally valid — a
    binary market's two tokens are complementary by construction.

    ``yes_asks`` / ``no_asks`` are raw CLOB ask levels (``asks_from_book``)."""
    return _arb(
        [yes_asks, no_asks],
        labels=["YES", "NO"],
        token_ids=[yes_token, no_token],
        kind="binary_complement",
        payout=payout,
        fee_bps=fee_bps,
        gas_usdc=gas_usdc,
        min_edge_usdc=min_edge_usdc,
        min_edge_bps=min_edge_bps,
    )


def field_buy_arb(
    outcome_asks: Sequence[Sequence[dict[str, Any]]],
    *,
    labels: Sequence[str] | None = None,
    token_ids: Sequence[str] | None = None,
    payout: float = 1.0,
    fee_bps: float = 0.0,
    gas_usdc: float = 0.0,
    min_edge_usdc: float = 0.0,
    min_edge_bps: float = 0.0,
) -> ArbOpportunity | None:
    """"Buy the field" arb on a MECE multi-outcome event: lift one YES of every
    outcome for less than the $1 the winning outcome is guaranteed to pay.

    CORRECTNESS PRECONDITION — the caller MUST guarantee the outcomes are
    mutually exclusive AND collectively exhaustive (on Polymarket: an event
    flagged ``negRisk=true``). This function cannot verify that; it trusts the
    set it is given. A non-exhaustive group yields a phantom arb.

    ``outcome_asks`` is one raw CLOB ask-level list per outcome (the outcome's
    YES token book)."""
    return _arb(
        outcome_asks,
        labels=labels,
        token_ids=token_ids,
        kind="field_buy",
        payout=payout,
        fee_bps=fee_bps,
        gas_usdc=gas_usdc,
        min_edge_usdc=min_edge_usdc,
        min_edge_bps=min_edge_bps,
    )
