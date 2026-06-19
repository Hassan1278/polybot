"""Market categorization — map a Polymarket market to one of our buckets.

Stages, in priority order:

  0. CARVE-OUTS (worldcup, weather) — matched by keyword on question+slug and
     checked BEFORE tags, so e.g. a World-Cup match tagged generic "soccer"
     still routes to `worldcup` (we want only World-Cup soccer, not all soccer),
     and temperature markets route to `weather`.
  1. Authoritative TAG match against the configured tag->category map (routes
     genuine sports etc. to their bucket — still gate-blocked).
  2. KEYWORD fallback on question+slug for the core trading categories
     (politics / crypto / macro), only when no tag matched.

Used by both the JIT resolver (market_resolver) and the bulk ingest so the two
paths can't drift. Keyword lists favour recall while staying specific; tokenized
matching means "fed" can't hit "federer".
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# High-priority carve-outs — win even over a sports/soccer tag. (set, phrases)
_CARVEOUTS: dict[str, tuple[set[str], tuple[str, ...]]] = {
    "worldcup": (
        {"fifwc", "worldcup"},
        ("world cup", "fifa world cup"),
    ),
    "weather": (
        {"temperature", "weather", "rainfall", "snowfall", "celsius", "fahrenheit"},
        ("highest temperature", "lowest temperature", "high temperature",
         "low temperature"),
    ),
}

# Core trading categories — keyword fallback (stage 2), checked only if no tag.
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
    # Low-priority catch-all for recognizable sports / esports — checked LAST
    # so politics/crypto/macro always win a tie. Tag-based classification still
    # produces sports_major for tagged majors; this recovers the untagged ones
    # (your tag recall on sports was ~0). All sports get the same gate treatment.
    "sports_other": (
        {
            "nfl", "nba", "nhl", "mlb", "ufc", "mma", "boxing", "tennis", "atp",
            "wta", "golf", "pga", "cricket", "rugby", "nascar", "esports", "cs2",
            "dota", "valorant",
        },
        (
            "counter-strike", "league of legends", "premier league",
            "champions league", "super bowl", "world series", "stanley cup",
            "grand slam", "formula 1", "grand prix", "la liga", "serie a",
            "bundesliga",
        ),
    ),
}


def _match(tokens: set[str], text: str, words: set[str], phrases: tuple[str, ...]) -> bool:
    return bool(tokens & words) or any(p in text for p in phrases)


# Novelty / low-information markets we never want to trade, regardless of how
# they're tagged — they slip into a tradable category via a broad tag (e.g. a
# market tagged "world" mapping to politics). A question/slug substring match
# forces "unclassified" (-> blocked by the category gate). Extend as needed.
_EXCLUDE_KW = ("tweet", "tweets")

# Dedicated novelty tag-buckets Polymarket uses that we never want to trade,
# even when the market ALSO carries a legit tag like `politics` (the tweet-count
# markets are tagged BOTH `politics` and `tweets-markets`). Any of these tags
# forces the market unclassified -> blocked by the category gate.
_EXCLUDE_TAGS = {"tweets-markets"}


def _excluded(question: str | None, slug: str | None) -> bool:
    text = f"{question or ''} {slug or ''}".lower()
    return any(k in text for k in _EXCLUDE_KW)


def classify_keywords(question: str | None, slug: str | None) -> str | None:
    """Keyword-only classification: carve-outs first, then core categories.
    Used by the backfill (no tags stored — only question + slug)."""
    if _excluded(question, slug):
        return None
    text = f"{question or ''} {slug or ''}".lower().strip()
    if not text:
        return None
    tokens = set(_TOKEN_RE.findall(text))
    for cat, (words, phrases) in _CARVEOUTS.items():
        if _match(tokens, text, words, phrases):
            return cat
    for cat, (words, phrases) in _KW.items():
        if _match(tokens, text, words, phrases):
            return cat
    return None


def classify_market(
    *,
    tags: list[str] | None,
    question: str | None,
    slug: str | None,
    tag_map: dict[str, list[str]],
) -> str | None:
    """Full classification. ``tag_map`` is category -> [tag-slugs] (already
    filtered to enabled categories by the caller)."""
    # Novelty exclusion wins over EVERYTHING (tags included) — these slip in via
    # broad tags (e.g. tweet markets tagged `politics`), so block them up front
    # by content keyword OR by a dedicated novelty tag.
    if _excluded(question, slug):
        return None
    if tags and _EXCLUDE_TAGS.intersection(str(t).lower() for t in tags):
        return None
    text = f"{question or ''} {slug or ''}".lower().strip()
    tokens = set(_TOKEN_RE.findall(text)) if text else set()

    # Stage 0 — carve-outs win even over a generic sports/soccer tag.
    for cat, (words, phrases) in _CARVEOUTS.items():
        if _match(tokens, text, words, phrases):
            return cat

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

    # Stage 2 — keyword fallback for the core trading categories.
    for cat, (words, phrases) in _KW.items():
        if _match(tokens, text, words, phrases):
            return cat
    return None
