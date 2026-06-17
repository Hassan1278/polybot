"""Thin wrapper around `py_clob_client_v2`.

For read-only orderbook data we use httpx directly (no key needed, fewer deps
loaded in services that never trade). The signing/order paths defer to the
official client.
"""

from __future__ import annotations

from typing import Any

from polybot.clients._http import HttpClient
from polybot.config import settings
from polybot.logging import get_logger

log = get_logger(__name__)

# Module-level sync engine cache. We need ONE sync SQLAlchemy engine
# across the entire process for the wallet-credential decrypt path;
# previously each ClobClient signing call created a fresh
# `create_engine(...)` and never disposed it, leaking ~5 pooled
# connections per call. Postgres's `max_connections` (default 100) would
# trip after ~20 live signals.
_SYNC_ENGINE = None
_SYNC_SESSION_FACTORY = None


def _get_sync_session_factory():
    global _SYNC_ENGINE, _SYNC_SESSION_FACTORY
    if _SYNC_SESSION_FACTORY is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        # The async URL works for sync too (psycopg backend covers both).
        # NOTE: no `.replace("+psycopg", "+psycopg")` — that was a typo
        # no-op in the old code.
        _SYNC_ENGINE = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=2,         # tiny — wallet decrypt is rare
            max_overflow=2,
            pool_recycle=300,
        )
        _SYNC_SESSION_FACTORY = sessionmaker(_SYNC_ENGINE, expire_on_commit=False)
    return _SYNC_SESSION_FACTORY


class ClobClient(HttpClient):
    def __init__(self) -> None:
        super().__init__(settings.polymarket_clob_url)
        self._signed = None  # lazily built when a signing call comes in

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

    # ---- signed ------------------------------------------------------------
    #
    # Defer to the official py-clob-client-v2 SDK only when we actually need to
    # sign. Built lazily so paper-mode never imports/initialises it.

    def _signed_client(self):  # type: ignore[no-untyped-def]
        if self._signed is not None:
            return self._signed

        try:
            from py_clob_client_v2.client import ClobClient as _Sdk  # type: ignore
        except ImportError as exc:
            raise RuntimeError("py-clob-client-v2 not installed — run `pip install py-clob-client-v2`") from exc

        # Prefer DB-stored wallet credential (encrypted, dashboard-managed).
        # Fall back to .env-based key only if DB has no active credential —
        # this keeps the legacy paper-test wiring alive while we migrate.
        creds = self._load_active_wallet_credential()
        if creds is not None:
            key_b = creds["private_key"]
            key = key_b.decode("utf-8") if isinstance(key_b, bytes) else key_b
            funder = creds["funder_address"]
            sig_type = creds["signature_type"]
            log.info("clob_signed_client_using_db_credential",
                     wallet_id=creds["id"], funder=funder)
        elif settings.can_sign:
            key = settings.polymarket_private_key.get_secret_value()
            funder = settings.polymarket_funder_address
            sig_type = settings.polymarket_signature_type
            log.warning("clob_signed_client_using_env_fallback", funder=funder,
                        msg="DB has no active wallet — using .env (deprecated)")
        else:
            raise RuntimeError(
                "no signing credential available — add a wallet via "
                "POST /admin/settings/wallet or set POLYMARKET_PRIVATE_KEY"
            )

        c = _Sdk(
            host=settings.polymarket_clob_url,
            key=key,
            chain_id=settings.polygon_chain_id,
            signature_type=sig_type,
            funder=funder,
        )
        c.set_api_creds(c.create_or_derive_api_creds())
        self._signed = c
        log.info("clob_signed_client_ready", funder=funder)
        return c

    @staticmethod
    def _load_active_wallet_credential() -> dict | None:
        """Sync helper that loads + decrypts the active wallet from the
        `wallet_credentials` table. Runs in a fresh sync connection (the
        py-clob-client SDK is sync, so we're already on a worker thread
        when this is called). Returns None if no active credential exists
        or decryption fails — caller falls back to env.

        The sync engine is process-cached (via `_get_sync_engine`) so each
        call reuses one pool instead of leaking ~5 connections per call —
        a previous version created a fresh `create_engine(...)` per
        invocation and never disposed it, draining Postgres `max_connections`
        on a high-fill day.
        """
        try:
            from datetime import datetime, timezone

            from sqlalchemy import select

            from polybot.crypto import decrypt
            from polybot.models.wallet_credential import WalletCredential

            session_local = _get_sync_session_factory()
            with session_local() as s:
                row = s.execute(
                    select(WalletCredential)
                    .where(WalletCredential.is_active.is_(True))
                    .order_by(WalletCredential.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if row is None:
                    return None
                aad = f"wallet:{row.address.lower()}:signing".encode()
                key_bytes = decrypt(bytes(row.encrypted_private_key), aad=aad)
                # touch last_used_at for ops visibility
                row.last_used_at = datetime.now(tz=timezone.utc)
                s.commit()
                return {
                    "id": row.id,
                    "address": row.address,
                    "funder_address": row.funder_address,
                    "signature_type": row.signature_type,
                    "private_key": key_bytes,
                }
        except Exception:  # noqa: BLE001
            log.exception("clob_load_wallet_failed")
            return None

    async def place_limit(
        self, *, token_id: str, side: str, price: float, size: float, order_type: str = "GTC"
    ) -> dict[str, Any]:
        c = self._signed_client()
        # py-clob-client-v2 is sync — run in a thread so we don't block the loop.
        import asyncio
        from py_clob_client_v2.clob_types import OrderArgs  # type: ignore

        def _do() -> Any:
            args = OrderArgs(price=price, size=size, side=side, token_id=token_id)
            signed = c.create_order(args)
            return c.post_order(signed, order_type)

        return await asyncio.to_thread(_do)

    async def cancel(self, order_id: str) -> dict[str, Any]:
        c = self._signed_client()
        import asyncio
        return await asyncio.to_thread(c.cancel, order_id)

    async def cancel_all(self) -> dict[str, Any]:
        c = self._signed_client()
        import asyncio
        return await asyncio.to_thread(c.cancel_all)

    async def open_orders(self) -> list[dict[str, Any]]:
        c = self._signed_client()
        import asyncio
        return await asyncio.to_thread(c.get_orders)
