"""Intra-Polymarket statistical arbitrage.

A self-contained, paper-first strategy that exploits *structural* (not
predictive) price relationships within a single venue — Polymarket. The whole
edge is mechanical: a basket of tokens that is guaranteed to pay $1 should never
cost less than $1, net of fees. When it does, that gap is risk-free profit.

Built as a clean package inside polybot so it can reuse the battle-tested infra
(CLOB client, Market model, DB session, execution path) while staying isolated
enough to extract to a standalone repo later. See README.md for the full design.

Public surface is the pure no-arb core; the scanner wires it to live data.
"""

from __future__ import annotations

from services.statarb.persistence import PersistenceTracker, Tracked
from services.statarb.relations import (
    ArbLeg,
    ArbOpportunity,
    asks_from_book,
    bids_from_book,
    binary_complement_arb,
    field_buy_arb,
)

__all__ = [
    "ArbLeg",
    "ArbOpportunity",
    "PersistenceTracker",
    "Tracked",
    "asks_from_book",
    "bids_from_book",
    "binary_complement_arb",
    "field_buy_arb",
]
