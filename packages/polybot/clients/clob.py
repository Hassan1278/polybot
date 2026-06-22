"""Polymarket CLOB client.

Read-only orderbook data goes over httpx directly (no auth needed). The
signing/order path (place + cancel orders) is delegated to the `clob-rs`
sidecar over HTTP: the Python CLOB v2 SDK cannot sign for V2 deposit wallets
(POLY_1271), but the Rust SDK can. See docs/V2_LIVE_MIGRATION.md.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from polybot.clients._http import HttpClient
from polybot.config import settings
from polybot.logging import get_logger

log = get_logger(__name__)

# Order-signing sidecar (services/clob-rs). Reachable on the internal docker
# network; override for local runs via CLOB_RS_URL.
_CLOB_RS_URL = os.environ.get("CLOB_RS_URL", "http://clob-rs:8082").rstrip("/")


class ClobClient(HttpClient):
    def __init__(self) -> None:
        super().__init__(settings.polymarket_clob_url)

    # ---- public read-only --------------------------------------------------

    async def midpoint(self, token_id: str) -> float:
        """Mid price for a token. Returns 0.0 if no orderbook (= 404 from CLOB).

        We swallow the 404 internally so that `best_mark()` can fall through
        to `/last-trade-price`. Previously the exception propagated all the
        way up, retried 4× (wasted ~10 s per call), and broke the dashboard
        mark display for any resolved-but-pending market.
        """
        try:
            d = await self.get("/midpoint", params={"token_id": token_id})
        except Exception:  # noqa: BLE001
            return 0.0
        return float(d["mid"]) if d and "mid" in d else 0.0

    async def price(self, token_id: str, side: str) -> float:
        try:
            d = await self.get("/price", params={"token_id": token_id, "side": side})
        except Exception:  # noqa: BLE001
            return 0.0
        return float(d["price"]) if d and "price" in d else 0.0

    async def last_trade_price(self, token_id: str) -> float:
        """Last printed trade price for the token. Survives orderbook-empty
        states (e.g. resolved-but-pending markets where /midpoint returns
        'no orderbook') — the last trade is usually the resolution-reveal
        price 0.999 / 0.001 in those cases.

        Returns 0.0 if no trade has ever printed.
        """
        try:
            d = await self.get("/last-trade-price", params={"token_id": token_id})
        except Exception:  # noqa: BLE001
            return 0.0
        if not d:
            return 0.0
        # CLOB returns {"price": "0.999", "side": "SELL"} or similar
        raw = d.get("price") if isinstance(d, dict) else None
        if raw is None:
            return 0.0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    async def best_mark(self, token_id: str) -> float:
        """Best available mark for a token: midpoint if there's an orderbook,
        else last-trade-price. Returns 0.0 only if both are unavailable.

        Use this for mark-to-market display — /midpoint alone returns 0 for
        resolved markets, which is misleading.
        """
        mid = await self.midpoint(token_id)
        if mid > 0:
            return mid
        return await self.last_trade_price(token_id)

    async def book(self, token_id: str) -> dict[str, Any]:
        """Orderbook for a token. Returns empty dict if no orderbook exists
        (e.g. resolved markets that CLOB has wiped). Without this swallow,
        callers like the liquidity gate would crash with HTTPStatusError 404
        instead of just rejecting the candidate cleanly. Cf. B13/B14.
        """
        try:
            d = await self.get("/book", params={"token_id": token_id})
        except Exception:  # noqa: BLE001
            return {}
        return d or {}

    async def books(self, token_ids: list[str]) -> list[dict[str, Any]]:
        try:
            d = await self.post("/books", json=[{"token_id": t} for t in token_ids])
        except Exception:  # noqa: BLE001
            return []
        return d or []

    async def price_history(self, market_slug: str, *, interval: str = "1h", fidelity: int = 60) -> list[dict[str, Any]]:
        return await self.get(
            "/prices-history",
            params={"market": market_slug, "interval": interval, "fidelity": fidelity},
        )

    async def trades_market(self, market_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return await self.get("/trades", params={"market": market_id, "limit": limit})

    # ---- signed (delegated to the clob-rs sidecar) -------------------------
    #
    # CLOB V2 deposit-wallet (POLY_1271) order signing runs in the Rust
    # `clob-rs` service; these are thin HTTP calls to it. Paper mode never
    # reaches here, so the sidecar only needs to be up for live trading.

    async def place_limit(
        self, *, token_id: str, side: str, price: float, size: float, order_type: str = "GTC"
    ) -> dict[str, Any]:
        """Place a limit order via the clob-rs sidecar.

        Returns ``{"status", "orderID", "raw"}`` on success; raises
        RuntimeError with the venue's reason on rejection so the caller
        records it on the Fill. ``order_type`` is accepted for interface
        compatibility (the V2 limit path rests as GTC).
        """
        payload = {
            "token_id": str(token_id),
            "side": side.upper(),
            "price": f"{price}",
            "size": f"{size}",
        }
        async with httpx.AsyncClient(timeout=25.0) as c:
            r = await c.post(f"{_CLOB_RS_URL}/order", json=payload)
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {}
        status = str(data.get("status") or "").lower()
        if r.status_code >= 400 or status == "rejected" or data.get("success") is False:
            raise RuntimeError(data.get("error") or f"clob_rs_http_{r.status_code}")
        return {"status": status or "submitted", "orderID": data.get("order_id"), "raw": data}

    async def cancel(self, order_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{_CLOB_RS_URL}/cancel", json={"order_id": order_id})
        return r.json() if r.content else {}

    async def cancel_all(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{_CLOB_RS_URL}/cancel-all")
        return r.json() if r.content else {}

    async def open_orders(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{_CLOB_RS_URL}/orders")
        d = r.json() if r.content else []
        return d if isinstance(d, list) else []

    async def balance(self) -> dict[str, Any]:
        """Live pUSD collateral balance (in DOLLARS) for the deposit wallet, as
        the CLOB sees it (via the clob-rs sidecar). Shape:
        ``{"ok", "balance", "funder"}`` on success or ``{"ok": false, "error"}``.

        UNITS — IMPORTANT: the clob-rs sidecar returns the on-chain USDC balance
        in raw 6-decimal BASE UNITS (microUSDC) — e.g. "32845218.43" means ~$32.85.
        This method is the SINGLE chokepoint that converts to human dollars, so
        every consumer (the equity-drawdown breaker, the /live/account card) gets
        dollars. If clob-rs is EVER changed to return dollars itself, delete the
        conversion here and update this comment — double-converting silently
        re-breaks the live circuit breaker (it was reading a $33 account as $32.8M,
        so the 15% drawdown guard never tripped). Best-effort: never raises, so a
        sleeping sidecar degrades to an error dict.
        """
        try:
            async with httpx.AsyncClient(timeout=12.0) as c:
                r = await c.get(f"{_CLOB_RS_URL}/balance")
            data = r.json() if r.content else {"ok": False, "error": "empty response"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:140]}"}
        if isinstance(data, dict) and data.get("ok") and data.get("balance") is not None:
            data["balance"] = self._micro_usdc_to_dollars(data["balance"])
        return data

    @staticmethod
    def _micro_usdc_to_dollars(raw: Any) -> str:
        """Convert a clob-rs microUSDC balance (6-decimal base units) to a
        human-dollar string, e.g. "32845218.43" -> "32.845218". Returns a string
        to preserve the decimal-string contract (consumers float() it). On a parse
        failure, returns the raw value unchanged and logs once (balance() is
        best-effort and must never raise)."""
        try:
            dollars = float(str(raw)) / 1_000_000.0
        except (TypeError, ValueError):
            log.warning("balance_unit_anomaly", reason="unparseable", raw=str(raw)[:32])
            return str(raw)
        if dollars > 1e7:
            # Sentinel only (no behaviour change): no account here holds >$10M, so
            # this almost certainly means the raw value was NOT base units (sidecar
            # contract drift). Log loudly; the converted value is still returned.
            log.warning("balance_unit_anomaly", reason="implausibly_large",
                        dollars=dollars, raw=str(raw)[:32])
        return f"{dollars:.6f}"
