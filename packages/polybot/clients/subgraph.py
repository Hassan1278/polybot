"""Goldsky-hosted Polymarket subgraph(s). GraphQL over httpx."""

from __future__ import annotations

from typing import Any

import httpx

from polybot.config import settings
from polybot.logging import get_logger

log = get_logger(__name__)


class SubgraphClient:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or settings.goldsky_subgraph_url
        if not self.url:
            log.warning("subgraph_url_unset", hint="set GOLDSKY_SUBGRAPH_URL in .env")
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def query(self, gql: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        r = await self._client.post(self.url, json={"query": gql, "variables": variables or {}})
        r.raise_for_status()
        d = r.json()
        if "errors" in d:
            raise RuntimeError(f"subgraph error: {d['errors']}")
        return d["data"]

    # ---- convenience queries ----------------------------------------------

    async def user_trades(self, address: str, *, first: int = 1000, skip: int = 0) -> list[dict[str, Any]]:
        gql = """
        query($user: String!, $first: Int!, $skip: Int!) {
          enrichedOrderFilleds(
            first: $first, skip: $skip,
            where: { maker: $user }
            orderBy: timestamp, orderDirection: desc
          ) {
            id
            timestamp
            transactionHash
            maker
            taker
            makerAssetID
            takerAssetID
            makerAmountFilled
            takerAmountFilled
            price
            side
            market { id slug }
          }
        }
        """
        data = await self.query(gql, {"user": address.lower(), "first": first, "skip": skip})
        return data.get("enrichedOrderFilleds", [])

    async def market_trades(self, market_id: str, *, first: int = 1000) -> list[dict[str, Any]]:
        gql = """
        query($mkt: String!, $first: Int!) {
          enrichedOrderFilleds(
            first: $first,
            where: { market: $mkt }
            orderBy: timestamp, orderDirection: desc
          ) {
            id timestamp transactionHash maker side price
            makerAmountFilled takerAmountFilled
          }
        }
        """
        data = await self.query(gql, {"mkt": market_id, "first": first})
        return data.get("enrichedOrderFilleds", [])
