from __future__ import annotations

from sqlalchemy import select

from polybot.models import Market
from services.signals.conditions.base import GateContext, GateResult

# Hard ceiling on what the bot may EVER bet. The YAML/Redis `allow` list can
# NARROW within this set but can never broaden beyond it — a stray dashboard
# toggle or Redis override can't re-enable other sports behind the operator's
# back. `worldcup` and `weather` are deliberate carve-outs (see categorize.py)
# so we trade World-Cup soccer + weather markets WITHOUT opening all of sports.
# To change the permitted universe, edit THIS set (and rebuild the signals svc).
HARD_ALLOWED_CATEGORIES = {"macro", "politics", "crypto", "worldcup", "weather"}


class CategoryMatch:
    name = "category_match"
    type = "hard"

    def __init__(self, *, enabled: bool, params: dict):
        self.enabled = enabled
        configured = {str(c).lower() for c in (params.get("allow") or [])}
        # Effective allow = configured ∩ hard ceiling. Empty config => the full
        # ceiling. Config can only ever subtract from HARD_ALLOWED_CATEGORIES.
        self.allow = (
            (configured & HARD_ALLOWED_CATEGORIES)
            if configured else set(HARD_ALLOWED_CATEGORIES)
        )
        # With a hard category whitelist, an untagged market is by definition
        # NOT one of the allowed categories (it could be anything, incl. sports).
        # Default to rejecting it so "only these categories" actually holds; the
        # param can still force-pass uncategorized markets if explicitly True.
        self.allow_uncategorized = bool(params.get("allow_uncategorized", False))

    async def evaluate(self, ctx: GateContext) -> GateResult:
        if not self.enabled:
            return GateResult(self.name, self.type, True, "disabled")
        mid = ctx.candidate["market_id"]
        row = (await ctx.session.execute(select(Market.category).where(Market.market_id == mid))).first()
        cat = row[0] if row else None
        ctx.extra["category"] = cat
        if not cat:
            if self.allow_uncategorized:
                return GateResult(self.name, self.type, True, "uncategorized — permissive pass")
            return GateResult(self.name, self.type, False, "unknown_category")
        if str(cat).lower() not in self.allow:
            return GateResult(self.name, self.type, False, f"category_not_allowed:{cat}")
        return GateResult(self.name, self.type, True, f"category:{cat}")
