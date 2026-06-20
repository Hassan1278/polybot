"""MLB-only sports policy (operator request: "disable all sports except MLB").

Verifies `polybot.categorize`:
  * MLB markets route to `sports_mlb` (the lone gate-allowed sport) via tag,
    carve-out keyword, or untagged team-name fallback.
  * Every other sport routes to `sports_major` / `sports_other` (gate-blocked).
  * Precision: cross-sport-ambiguous names (Giants/...) and common words
    (Royals/...) never leak a non-MLB market into `sports_mlb`.

Pure functions — no DB/Redis needed.
"""

from __future__ import annotations

from polybot.categorize import classify_keywords, classify_market

# Mirrors the enabled-category tag_map the ingest/resolver build from
# config/categories.yaml (category -> [tag-slugs]).
TAG_MAP = {
    "politics": ["politics", "us-politics", "world"],
    "crypto": ["crypto", "bitcoin"],
    "macro": ["macro", "fed"],
    "sports_mlb": ["mlb", "baseball"],
    "sports_major": ["nfl", "nba", "nhl", "champions-league", "tennis-grandslam"],
    "sports_other": ["sports", "tennis", "soccer", "ufc", "boxing"],
}


def cm(question=None, slug=None, tags=None):
    return classify_market(tags=tags, question=question, slug=slug, tag_map=TAG_MAP)


# ── MLB stays tradable ────────────────────────────────────────────────────────

def test_mlb_tag_routes_to_sports_mlb():
    assert cm(question="Will the Yankees win tonight?", tags=["mlb"]) == "sports_mlb"
    assert cm(question="Astros vs Mariners", tags=["baseball"]) == "sports_mlb"


def test_mlb_carveout_beats_a_generic_sports_tag():
    # Mis-tagged as soccer, but unmistakably MLB -> carve-out (stage 0) wins.
    assert cm(question="MLB: Dodgers vs Padres tonight", tags=["soccer"]) == "sports_mlb"


def test_world_series_phrase_is_mlb():
    assert cm(question="Who wins the 2026 World Series?", tags=None) == "sports_mlb"


def test_untagged_mlb_team_names_recovered():
    assert cm(question="Yankees vs Red Sox on June 20?", tags=None) == "sports_mlb"
    assert cm(question="Will the Orioles beat the Blue Jays?", tags=None) == "sports_mlb"


# ── Every other sport is NOT mlb (-> gate-blocked bucket) ─────────────────────

def test_other_sports_route_to_blocked_buckets():
    assert cm(question="Chiefs vs Eagles", tags=["nfl"]) == "sports_major"
    assert cm(question="Lakers vs Celtics", tags=["nba"]) == "sports_major"
    assert cm(question="Sabalenka vs Pegula", tags=["tennis"]) == "sports_other"
    assert cm(question="UFC 320 main event", tags=["ufc"]) == "sports_other"


# ── Precision: no non-MLB market may sneak into the one allowed sport ─────────

def test_cross_sport_name_does_not_leak_to_mlb():
    # NFL Giants stay sports_major via tag; untagged "Giants" is a safe miss
    # (None), never mislabeled MLB.
    assert cm(question="New York Giants vs Cowboys", tags=["nfl"]) == "sports_major"
    assert cm(question="Giants vs Cowboys this Sunday", tags=None) != "sports_mlb"


def test_common_word_team_names_excluded():
    # "royals"/"guardians" deliberately omitted so a monarchy / generic market
    # can't be mislabeled MLB.
    assert cm(question="Will the royals attend the coronation?", tags=None) != "sports_mlb"
    assert cm(question="Guardians of the Galaxy box office record?", tags=None) != "sports_mlb"


# ── Strict MLB: other baseball leagues are blocked, not just other sports ─────

def test_non_mlb_baseball_leagues_blocked():
    # Korean (KBO) and Japanese (NPB) pro baseball ride in on the `baseball`
    # tag but are NOT Major League -> must NOT route to sports_mlb.
    assert cm(question="KBO: Kia Tigers vs. KT Wiz", tags=["baseball"]) != "sports_mlb"
    assert cm(question="NPB: Yomiuri Giants vs. Hanshin Tigers", tags=["baseball"]) != "sports_mlb"


def test_college_and_amateur_baseball_blocked():
    # "College World Series" matches the bare "world series" phrase but is NCAA,
    # not MLB -> blocked.
    assert cm(question="Will Oklahoma win the 2026 College World Series?", tags=None) != "sports_mlb"
    assert cm(question="Little League World Series winner", tags=["baseball"]) != "sports_mlb"
    assert cm(question="2026 World Baseball Classic champion", tags=["baseball"]) != "sports_mlb"


def test_real_mlb_world_series_still_allowed():
    # Regression guard: the strict-MLB veto must NOT block actual MLB markets.
    assert cm(question="Will the Chicago Cubs win the 2026 World Series?", tags=None) == "sports_mlb"
    assert cm(question="Yankees vs Red Sox", tags=["baseball"]) == "sports_mlb"


# ── Non-sports categories unaffected ─────────────────────────────────────────

def test_core_categories_unchanged():
    assert cm(question="Will Trump win?", tags=["politics"]) == "politics"
    assert cm(question="Bitcoin above $100k?", tags=["crypto"]) == "crypto"


# ── Backfill keyword-only path ───────────────────────────────────────────────

def test_keyword_path_mlb_and_other_sports():
    assert classify_keywords("2026 World Series winner", None) == "sports_mlb"
    assert classify_keywords("Dodgers vs Padres", None) == "sports_mlb"
    # Non-MLB sport still recognized (and gate-blocked downstream).
    assert classify_keywords("Chiefs vs Eagles NFL game", None) == "sports_other"
