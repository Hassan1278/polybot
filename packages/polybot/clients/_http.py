"""Shared httpx-based client base with retry + structured logging."""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from polybot.logging import get_logger

log = get_logger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    """Retry on connection errors, timeouts, and 5xx — NOT on 4xx.

    A 404 from /midpoint is "no orderbook for this market" — that's a
    permanent state, not a transient one. Retrying 4× on every resolved
    market multiplies the per-call latency by ~16× (sum of 0.5+1+2+4 s
    backoff) and burns the caller's timeout budget for nothing. 429 is
    the exception — that IS transient and we do retry it.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        sc = exc.response.status_code
        return sc == 429 or sc >= 500
    # Connection/timeout/protocol errors are always retryable
    return isinstance(exc, httpx.HTTPError)


class HttpClient:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"user-agent": "polybot/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        reraise=True,
    )
    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._client.request(method, path, **kw)
        if r.status_code == 429:
            log.warning("rate_limited", base=self.base_url, path=path)
            raise httpx.HTTPError("429 rate limited")
        r.raise_for_status()
        if not r.content:
            return None
        return r.json()

    async def get(self, path: str, **kw: Any) -> Any:
        return await self._req("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self._req("POST", path, **kw)
