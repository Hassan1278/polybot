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

import math
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

# Correlated large caps the cross-asset directional guard treats as ONE book per
# timeframe (see services/executor/risk.py:_crypto_timeframe_conflict). Memecoins
# (DOGE/SHIB/PEPE) are excluded — they decouple from BTC often enough that forcing
# a shared direction would wrongly block legitimate independent bets. Every entry
# must be a key in _ASSETS above so asset_of() can actually return it.
CRYPTO_MAJORS: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT", "LINK", "LTC", "MATIC", "TRX",
})

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


# --- Range / volatility markets ("between $A and $B") ----------------------
# A range market has no bull/bear direction — it's a volatility bet:
#   YES between $A-$B  -> price PINS inside the band   (stance "in")
#   NO  between $A-$B  -> price BREAKS OUT of the band (stance "out")
# Holding both stances on the SAME asset+band is the two-sided bet we avoid, so
# we expose the band + stance for the per-asset guard (separate axis from
# bull/bear — a directional bet and a range bet don't conflict with each other).

_BETWEEN_RE = re.compile(
    r"between\s*\$?\s*([\d,]+(?:\.\d+)?)\s*([km])?\s*(?:and|to|-|–|&)\s*"
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*([km])?",
    re.I,
)


def _to_number(num: str, suffix: str | None) -> float | None:
    try:
        val = float(str(num).replace(",", ""))
    except (TypeError, ValueError):
        return None
    s = (suffix or "").lower()
    if s == "k":
        val *= 1_000
    elif s == "m":
        val *= 1_000_000
    return val


def range_bet(question: str | None, slug: str | None,
              outcome: str | None, side: str | None) -> tuple[str, float, float] | None:
    """For a 'between $A and $B' range market, return (stance, low, high):
    stance 'in' (YES pins in band) or 'out' (NO breaks out); SELL flips. None if
    it isn't a parseable range market."""
    text = f"{question or ''} {slug or ''}".lower()
    m = _BETWEEN_RE.search(text)
    if not m:
        return None
    low = _to_number(m.group(1), m.group(2))
    high = _to_number(m.group(3), m.group(4))
    if low is None or high is None:
        return None
    if low > high:
        low, high = high, low
    o = (outcome or "").strip().upper()
    if o == "YES":
        stance = "in"
    elif o == "NO":
        stance = "out"
    else:
        return None
    if (side or "").strip().upper() == "SELL":
        stance = "out" if stance == "in" else "in"
    return (stance, low, high)


def same_bracket(a: tuple[str, float, float], b: tuple[str, float, float],
                 *, tol: float = 0.005) -> bool:
    """True if two range bets cover the same price band (endpoints within `tol`
    relative tolerance) — so daily reissues of one band match across dates."""
    _, alo, ahi = a
    _, blo, bhi = b

    def close(x: float, y: float) -> bool:
        return abs(x - y) <= tol * max(abs(x), abs(y), 1.0)

    return close(alo, blo) and close(ahi, bhi)


# --- Win-region model (threshold AND range, unified) -----------------------
# A directional THRESHOLD bet ("above $X") and a volatility RANGE bet
# ("between $A-$B") look like different axes, but they can still contradict:
# holding "above 62k NO" (wins <=62k) alongside "between 60-62k NO" (wins <60k
# OR >62k) is a self-hedge that bleeds fees. To compare ANY two crypto price
# bets we reduce each to the set of final-price intervals where it WINS, then
# check whether the two "cross" (each wins where the other loses).

# A price level: $-prefixed (any magnitude) OR a k/m-suffixed number. Bare
# integers (dates, years) are deliberately NOT matched — precision over recall.
_PRICE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([km])?|([\d,]+(?:\.\d+)?)\s*([km])\b", re.I)


def _threshold_price(text: str | None, *, tol: float = 0.005) -> float | None:
    """The single price level named by a threshold market, or None.

    Returns a value when every price-like token in the text agrees on ONE level
    (within `tol` — the question and slug often both spell it, e.g. "$62,000" +
    "62k"). Zero matches (no parseable level) or genuinely DIFFERENT levels (a
    compound threshold) are ambiguous → None."""
    prices: list[float] = []
    for m in _PRICE_RE.finditer(text or ""):
        val = (_to_number(m.group(1), m.group(2)) if m.group(1) is not None
               else _to_number(m.group(3), m.group(4)))
        if val is not None:
            prices.append(val)
    if not prices:
        return None
    lo, hi = min(prices), max(prices)
    return prices[0] if hi - lo <= tol * max(abs(hi), 1.0) else None


def win_region(question: str | None, slug: str | None,
               outcome: str | None, side: str | None) -> list[tuple[float, float]] | None:
    """Price intervals where buying/selling ``outcome`` WINS, or None when the
    market isn't a parseable crypto price bet (fail-open, like its siblings).

    Open ends use +/-inf. Range markets win inside ('in') or outside ('out') a
    band; threshold markets win above ('bull') or below ('bear') a single level
    — direction() supplies bull/bear and already applies the SELL flip."""
    rng = range_bet(question, slug, outcome, side)
    if rng is not None:
        stance, lo, hi = rng
        if stance == "in":
            return [(lo, hi)]
        return [(-math.inf, lo), (hi, math.inf)]
    d = direction(question, slug, outcome, side)
    if d is None:
        return None
    x = _threshold_price(f"{question or ''} {slug or ''}")
    if x is None:
        return None
    return [(x, math.inf)] if d == "bull" else [(-math.inf, x)]


def _subtract(a_list: list[tuple[float, float]],
              b_list: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """A \\ B for two unions of intervals (inf-aware)."""
    out: list[tuple[float, float]] = []
    for lo, hi in a_list:
        pieces = [(lo, hi)]
        for blo, bhi in b_list:
            nxt: list[tuple[float, float]] = []
            for lo2, hi2 in pieces:
                if bhi <= lo2 or blo >= hi2:        # no overlap
                    nxt.append((lo2, hi2))
                    continue
                if blo > lo2:
                    nxt.append((lo2, blo))
                if bhi < hi2:
                    nxt.append((bhi, hi2))
            pieces = nxt
        out.extend(pieces)
    return out


def regions_conflict(a: list[tuple[float, float]], b: list[tuple[float, float]],
                     *, rel_tol: float = 0.005) -> bool:
    """True if two win-regions CROSS — each wins on an interval (wider than a
    relative tolerance) where the other loses. Nested / identical / refinement
    regions never conflict. The tolerance mirrors same_bracket() so a daily band
    reissue that differs by < rel_tol isn't flagged as a new conflicting band."""
    scale = max((abs(p) for ivs in (a, b) for iv in ivs for p in iv if math.isfinite(p)),
                default=1.0)
    eps = rel_tol * max(scale, 1.0)

    def has_wide(ivs: list[tuple[float, float]]) -> bool:
        return any(hi - lo > eps for lo, hi in ivs)

    return has_wide(_subtract(a, b)) and has_wide(_subtract(b, a))
