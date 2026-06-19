"""Crypto asset + price-direction inference for the one-sided-per-asset guard.

The bot mirrors smart money across MANY crypto markets at once. Left alone it
will happily hold "BTC up today" *and* "BTC below $X" — opposite bets on the
same underlying that lock in a guaranteed fee bleed. This module extracts, from
a market's question/slug, (a) the canonical asset symbol and (b) whether a given
(outcome, side) is a *bullish* or *bearish* bet on that asset, so risk.py can
refuse a new order that contradicts an open position on the same asset.

Design rule: PRECISION over recall. Every function returns None the moment the
text is ambiguous (compound thresholds, unknown outcome labels, no asset). The
caller fails OPEN on None — a missed inference just behaves like today; a *wrong*
inference would wrongly block a legitimate trade, which is the costly error.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Canonical symbol -> identifying tokens (matched as whole tokens, so "eth"
# can't hit "ethereum" twice and "sol" can't hit "solid"). Order doesn't matter;
# a market mentioning two assets is treated as ambiguous (see asset_of).
_ASSETS: dict[str, set[str]] = {
    "BTC": {"bitcoin", "btc"},
    "ETH": {"ethereum", "ether", "eth"},
    "SOL": {"solana", "sol"},
    "DOGE": {"dogecoin", "doge"},
    "XRP": {"xrp", "ripple"},
    "ADA": {"cardano", "ada"},
    "BNB": {"bnb"},
    "AVAX": {"avalanche", "avax"},
    "LINK": {"chainlink"},
    "LTC": {"litecoin", "ltc"},
    "DOT": {"polkadot"},
    "SHIB": {"shiba", "shib"},
    "PEPE": {"pepe"},
    "MATIC": {"polygon", "matic"},
    "TRX": {"tron", "trx"},
}

# Bullish = a bet the price goes UP. Tokens matched whole; phrases as substrings.
_BULL_WORDS = {
    "above", "over", "reach", "reaches", "reached", "hit", "hits", "exceed",
    "exceeds", "surpass", "surpasses", "higher", "greater", "rise", "rises",
    "rising", "rally", "rallies", "moon", "ath", "gain", "gains", "climb",
    "climbs", "soar", "soars",
}
_BULL_PHRASES = (
    "more than", "greater than", "higher than", "at least", "all time high",
    "all-time high", "record high", "new high", "go up", "going up",
)

# Bearish = a bet the price goes DOWN.
_BEAR_WORDS = {
    "below", "under", "beneath", "dip", "dips", "fall", "falls", "fell",
    "drop", "drops", "dropped", "crash", "crashes", "lower", "decline",
    "declines", "sink", "sinks", "plunge", "plunges", "tumble", "slump",
}
_BEAR_PHRASES = (
    "less than", "lower than", "go down", "going down", "fall below",
    "drop below", "dip below", "all time low", "all-time low", "record low",
    "new low",
)


def asset_of(question: str | None, slug: str | None = None) -> str | None:
    """Canonical crypto symbol named by the market, or None.

    Returns None if no known asset is mentioned OR if more than one is (a
    cross-asset market like "ETH/BTC ratio" has no single direction)."""
    text = f"{question or ''} {slug or ''}".lower()
    if not text.strip():
        return None
    tokens = set(_TOKEN_RE.findall(text))
    hits = {sym for sym, toks in _ASSETS.items() if tokens & toks}
    if len(hits) != 1:
        return None
    return next(iter(hits))


def direction(question: str | None, slug: str | None,
              outcome: str | None, side: str | None) -> str | None:
    """'bull' | 'bear' | None — the directional exposure of buying/selling
    ``outcome``. None whenever the text/label is ambiguous (fail-open signal)."""
    o = (outcome or "").strip().upper()

    # Up/Down markets — the outcome label itself is authoritative, no text needed.
    if o in ("UP", "DOWN"):
        bull = (o == "UP")
    elif o in ("YES", "NO"):
        text = f"{question or ''} {slug or ''}".lower()
        tokens = set(_TOKEN_RE.findall(text))
        is_bull = bool(tokens & _BULL_WORDS) or any(p in text for p in _BULL_PHRASES)
        is_bear = bool(tokens & _BEAR_WORDS) or any(p in text for p in _BEAR_PHRASES)
        if is_bull == is_bear:  # neither, or BOTH (compound) -> ambiguous
            return None
        yes_is_bull = is_bull
        bull = yes_is_bull if o == "YES" else (not yes_is_bull)
    else:
        return None  # multi-outcome / unknown label

    # Selling an outcome is the opposite exposure to buying it.
    if (side or "").strip().upper() == "SELL":
        bull = not bull
    return "bull" if bull else "bear"
