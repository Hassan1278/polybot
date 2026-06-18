"""Data API — user positions, trades, activity, leaderboards. Public."""

from __future__ import annotations

from typing import Any

from polybot.clients._http import HttpClient
from polybot.config import settings


class DataClient(HttpClient):
    def __init__(self) -> None:
        super().__init__(settings.polymarket_data_url)

    # ---- discovery via market trades ---------------------------------------
    #
    # Polymarket no longer exposes a public leaderboard JSON endpoint. We
    # discover wallets indirectly by aggregating recent trades on the busiest
    # markets and ranking participants by traded notional.

    async def market_trades(
        self,
        market_id: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return await self.get(
            "/trades",
            params={"market": market_id, "limit": limit, "offset": offset},
        )

    # ---- per-user ----------------------------------------------------------

    async def positions(
        self,
        user: str,
        *,
        limit: int = 100,
        offset: int = 0,
        size_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        # The data API hides positions below `sizeThreshold` (default 1.0
        # share). Pass a small threshold to also surface sub-1-share live
        # positions, which a tiny test/initial position can be.
        params: dict[str, Any] = {"user": user, "limit": limit, "offset": offset}
        if size_threshold is not None:
            params["sizeThreshold"] = size_threshold
        return await self.get("/positions", params=params)

    async def trades(self, user: str, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        return await self.get(
            "/trades",
            params={"user": user, "limit": limit, "offset": offset},
        )

    async def activity(self, user: str, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        return await self.get(
            "/activity",
            params={"user": user, "limit": limit, "offset": offset},
        )

    async def user(self, address: str) -> dict[str, Any]:
        return await self.get(f"/user/{address}")

    # ---- markets ----------------------------------------------------------

    async def holders(self, market_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return await self.get(f"/holders/{market_id}", params={"limit": limit})
