"""Market categorization — map a Polymarket market to one of our buckets.

Two-stage, so in-scope markets don't get dropped just because Gamma's tags are
sparse or non-standard:

  1. Authoritative TAG match against the configured tag->category map (covers
     ALL configured buckets, so genuine sports etc. route to their bucket and
     get blocked by the category gate).
  2. KEYWORD fallback on the market question + slug, for the three TRADING
     categories only (politics / crypto / macro). Runs only when no tag matched.

Used by both the JIT resolver (market_resolver) and the bulk ingest so the two
paths can't drift. The keyword lists favour recall (trade everything that's
really politics/macro/crypto) while staying specific enough to avoid pulling in
off-topic markets — and stage 1 catches tagged sports before stage 2 ever runs.
"""

from __future__ import annotations

import re

# Whole-token keywords (matched against the tokenized text, so "fed" can't hit
# "federer") plus multi-word phrases (plain substring). Dict order = priority
# when a market matches more than one (all three are allowed anyway).
_KW: dict[str, tuple[set[str], tuple[str, ...]]] = {
    "politics": (
        {
            "trump", "biden", "harris", "kamala", "desantis", "newsom", "vance",
            "obama", "pence", "haley", "ramaswamy", "election", "elections",
            "electoral", "senate", "senator", "congress", "congressional",
            "president", "presidential", "republican", "republicans", "democrat",
            "democrats", "gop", "governor", "primary", "primaries", "ballot",
            "impeach", "impeachment", "scotus", "putin", "zelensky", "nato",
            "parliament", "midterm", "midterms", "nominee", "nomination", "mayor",
            "referendum", "geopolitics", "coup", "sanctions", "ukraine", "israel",
        },
        (
            "supreme court", "white house", "prime minister", "government shutdown",
            "us politics", "presidential election", "speaker of the house",
            "secretary of", "electoral college",
        ),
    ),
    "crypto": (
        {
            "bitcoin", "btc", "ethereum", "eth", "solana", "crypto",
            "cryptocurrency", "blockchain", "defi", "dogecoin", "doge", "xrp",
            "ripple", "cardano", "binance", "bnb", "coinbase", "stablecoin",
            "stablecoins", "memecoin", "memecoins", "altcoin", "altcoins", "nft",
            "nfts", "satoshi", "microstrategy", "litecoin", "polkadot",
            "avalanche", "avax", "chainlink", "shiba", "pepe", "tether",
        },
        ("bitcoin etf", "ethereum etf", "crypto etf", "spot etf"),
    ),
    "macro": (
        {
            "fed", "inflation", "cpi", "pce", "gdp", "recession", "unemployment",
            "powell", "economy", "economic", "treasury", "tariff", "tariffs",
            "fomc", "deflation", "stagflation", "payrolls", "yields",
        },
        (
            "federal reserve", "interest rate", "interest rates", "rate cut",
            "rate hike", "rate decision", "jobs report", "bond yield",
            "debt ceiling", "jerome powell", "non-farm", "nonfarm",
            "basis points", "soft landing",
        ),
    ),
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def classify_keywords(question: str | None, slug: str | None) -> str | None:
    """Keyword-only classification (stage 2). Used by the backfill, which has
    no tags stored — only question + slug."""
    text = f"{question or ''} {slug or ''}".lower().strip()
    if not text:
        return None
    tokens = set(_TOKEN_RE.findall(text))
    for cat, (words, phrases) in _KW.items():
        if tokens & words:
            return cat
        if any(p in text for p in phrases):
            return cat
    return None


def classify_market(
    *,
    tags: list[str] | None,
    question: str | None,
    slug: str | None,
    tag_map: dict[str, list[str]],
) -> str | None:
    """Full two-stage classification. ``tag_map`` is category -> [tag-slugs]
    (already filtered to enabled categories by the caller)."""
    # Stage 1 — authoritative Gamma tags.
    if tags:
        flat: dict[str, str] = {}
        for cat, tlist in tag_map.items():
            for t in (tlist or []):
                flat[str(t).lower()] = cat
        for t in tags:
            cat = flat.get(str(t).lower())
            if cat:
                return cat
    # Stage 2 — keyword fallback (trading categories only).
    return classify_keywords(question, slug)
