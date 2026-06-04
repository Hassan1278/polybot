"""Gamma API — markets, events, tags, search. No auth."""

from __future__ import annotations

from typing import Any

from polybot.clients._http import HttpClient
from polybot.config import settings


class GammaClient(HttpClient):
    def __init__(self) -> None:
        super().__init__(settings.polymarket_gamma_url)

    async def markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "volume24hr",
        ascending: bool = False,
        tag: str | None = None,
        include_tag: bool = True,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if tag:
            params["tag"] = tag
        if include_tag:
            params["include_tag"] = "true"
        return await self.get("/markets", params=params)

    async def market(self, market_id: str) -> dict[str, Any]:
        return await self.get(f"/markets/{market_id}")

    async def market_by_condition_id(self, condition_id: str) -> dict[str, Any] | None:
        """Fetch a single market by its on-chain conditionId. Returns None if
        not found. Used for just-in-time resolution when a trade points to a
        market we haven't bulk-ingested yet."""
        out = await self.get(
            "/markets",
            params={"condition_ids": condition_id, "include_tag": "true"},
        )
        if isinstance(out, list) and out:
            return out[0]
        return None

    async def events(self, *, limit: int = 100, offset: int = 0, active: bool = True) -> list[dict[str, Any]]:
        return await self.get(
            "/events",
            params={"limit": limit, "offset": offset, "active": str(active).lower()},
        )

    async def search(self, q: str, limit: int = 20) -> dict[str, Any]:
        return await self.get("/public-search", params={"q": q, "limit_per_type": limit})

    async def tags(self) -> list[dict[str, Any]]:
        return await self.get("/tags")
