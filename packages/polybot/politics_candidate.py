"""Candidate identity extraction for the one-position-per-politics-candidate guard.

The bot mirrors smart money across many politics markets and has no notion of
*which candidate* a market is about, so it will stack several (often
contradictory) bets on the same person across different markets/events — e.g. an
open "X win by 5-10%" position while it keeps placing "X not president" orders.
This module extracts a canonical candidate key from a market's question so
risk.py can refuse a new politics order on a candidate we already hold an open
position on. It keys on the NAME, so it links markets that share no Polymarket
event_id (which the one-position-per-event guard cannot).

Design rule (same as asset_direction.py): PRECISION over recall. candidate_of()
returns None the moment the text is ambiguous — no "Will <name> win/be ..." frame,
a generic label like "the Democrat", or an excluded name. The caller fails OPEN
on None: a missed extraction just behaves like today, whereas a *wrong* one would
block a legitimate unrelated trade, which is the costly error.
"""

from __future__ import annotations

import re
import unicodedata

# Candidates the rule never applies to. "Trump" appears across dozens of
# unrelated markets (2028 nomination, various elections, policy props, ...), so
# linking them all by name would wrongly block legitimate independent bets.
# Matched as a whole token within the extracted name. Easily editable.
POLITICS_CANDIDATE_EXCLUDE: frozenset[str] = frozenset({"trump"})

# Generic / non-name captures that must NOT be treated as a candidate identity.
_GENERIC: frozenset[str] = frozenset({
    "democrat", "democrats", "democratic", "republican", "republicans", "gop",
    "candidate", "nominee", "incumbent", "president", "party", "field", "winner",
    "he", "she", "they", "it", "anyone", "someone", "next",
})

# "Will <NAME> win/be/become ..." — capture the name between a leading
# interrogative and the first outcome verb. Non-greedy so it stops at the verb.
_LEAD_RE = re.compile(
    r"^\s*(?:will|would|could|can|does|do|is|are|has|have)\s+(.+?)\s+"
    r"(?:win|wins|won|be|become|becomes|became|beat|beats|defeat|defeats|"
    r"advance|advances|lose|loses|lost|concede|concedes|reach|reaches|"
    r"secure|secures|clinch|clinches|remain|remains|stay|stays|get|gets)\b",
    re.IGNORECASE,
)


def _normalize(name: str) -> str:
    """Lowercase, strip accents + punctuation, collapse whitespace, drop a
    leading article — so the same person matches across question phrasings."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(?:the|a|an)\s+", "", s)
    return s


def candidate_of(question: str | None, slug: str | None = None) -> str | None:
    """Canonical candidate key named by a politics market, or None.

    None when there's no "Will <name> win/be ..." frame, the captured phrase has
    no proper-noun token, it's a generic label (e.g. "the Democrat"), it's too
    long to be a real name, or it contains an excluded name (Trump). The caller
    fails open on None."""
    m = _LEAD_RE.match((question or "").strip())
    if not m:
        return None
    raw = m.group(1).strip()
    # Require at least one proper-noun-looking token (Capitalized, non-generic),
    # so phrases like "the next president" / "the Democrat" don't become a key.
    if not any(t[:1].isupper() and _normalize(t) not in _GENERIC for t in raw.split()):
        return None
    cand = _normalize(raw)
    if not cand:
        return None
    toks = cand.split()
    if len(toks) > 6:
        return None                                  # almost certainly a mis-parse
    if any(tok in POLITICS_CANDIDATE_EXCLUDE for tok in toks):
        return None                                  # Trump → rule does not apply
    if all(tok in _GENERIC for tok in toks):
        return None                                  # "the Democrat", "the incumbent"
    return cand
