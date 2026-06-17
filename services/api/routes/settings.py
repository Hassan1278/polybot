"""Admin-only endpoints for runtime settings + wallet management.

All endpoints require the admin token (HMAC-hardened or legacy plain,
see services/api/deps.py:require_admin). Mutations are audit-logged via
polybot.runtime_config helpers.

Layout:
  GET    /admin/settings                   effective config (yaml + redis merged)
  GET    /admin/settings/mode              current effective mode
  POST   /admin/settings/mode              switch paper<->live (requires confirm)
  PATCH  /admin/settings/risk              partial override of risk-config
  DELETE /admin/settings/risk              clear all risk overrides
  PATCH  /admin/settings/categories/{name} partial override of a category
  POST   /admin/settings/categories        add a category (tags, top_n, etc.)
  DELETE /admin/settings/categories/{name} soft-disable (sets enabled=false override)
  PATCH  /admin/settings/gates             partial override of gate params
  GET    /admin/settings/wallet            list configured wallets (NEVER returns key)
  POST   /admin/settings/wallet            encrypt+insert a new wallet
  DELETE /admin/settings/wallet/{id}       soft-delete (is_active=false)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from polybot.crypto import encrypt
from polybot.db import get_session, session_scope
from polybot.models import AuditLog
from polybot.logging import get_logger
from polybot.models import WalletCredential
from polybot.runtime_config import (
    clear_overrides,
    current_mode,
    enabled_modes,
    get_overrides,
    merged_categories,
    merged_gates,
    merged_risk,
    set_enabled_modes,
    set_mode,
    set_overrides,
)
from polybot.yaml_config import categories_cfg, gates_cfg, risk_cfg
from services.api.deps import require_admin

log = get_logger(__name__)
router = APIRouter()

# Live-mode switch needs a fresh dedicated HMAC distinct from the normal admin
# token (defense in depth: a leaked admin token alone can't flip the bot to
# live mode). The dashboard generates this by HMAC'ing
# "switch-to-live:{epoch}" with the admin secret, sending via X-Live-Confirm.
_LIVE_SWITCH_SKEW_S = 60


def _client_ip(req: Request) -> str:
    """Resolve client IP for audit-log attribution.

    Delegates to services.api.rate_limit._client_ip which already enforces
    the TRUSTED_PROXIES allowlist. The naive XFF-then-fallback pattern
    that lived here let an attacker spoof the audit log's actor field —
    a critical accountability gap when post-incident review needs to
    know who flipped LIVE / added a wallet / cleared the kill switch.
    """
    from services.api.rate_limit import _client_ip as _safe_client_ip
    return _safe_client_ip(req)


# ---- Effective settings read ----------------------------------------------


# Register with empty path so the full route is `/admin/settings` (no
# trailing slash). The dashboard's fetcher calls `/admin/settings` which
# now lands directly — no redirect, no 308/307 loop. Old callers that
# include the trailing slash still work via FastAPI's redirect_slashes.
@router.get("", dependencies=[Depends(require_admin)])
async def get_effective_settings() -> dict[str, Any]:
    """Return the current effective config (yaml baseline + Redis overrides),
    plus the raw overrides so the dashboard can show diff badges.

    Secrets are NEVER included (no private keys, no encryption-key bytes).
    """
    mode = await current_mode()
    return {
        "mode": mode,
        "effective": {
            "risk": await merged_risk(mode),
            "categories": await merged_categories(mode),
            "gates": await merged_gates(mode),
        },
        "overrides": {
            "risk": await get_overrides("risk", mode),
            "categories": await get_overrides("categories", mode),
            "gates": await get_overrides("gates", mode),
        },
        "baseline": {
            # raw yaml so dashboard can show the "factory default" alongside
            "risk": risk_cfg.get(mode),
            "categories": (categories_cfg.get(mode) or {}).get("categories", {}),
            "gates": gates_cfg.get(mode),
        },
    }


@router.get("/mode", dependencies=[Depends(require_admin)])
async def get_mode_endpoint() -> dict[str, Any]:
    """Return both the legacy single-mode label AND the parallel
    `enabled_modes` set so the dashboard can render the new dual-toggle
    UI while older clients still see `{"mode": "paper"}`.
    """
    return {
        "mode": await current_mode(),
        "enabled_modes": sorted(await enabled_modes()),
    }


class EnabledModesPatch(BaseModel):
    paper: bool | None = None
    live:  bool | None = None


@router.patch("/mode/enabled", dependencies=[Depends(require_admin)])
async def patch_enabled_modes(
    body: EnabledModesPatch, request: Request,
    x_live_confirm: str | None = Header(default=None),
) -> dict[str, Any]:
    """Patch the active mode SET (parallel paper+live).

    Enabling `live` requires the same HMAC + live-readiness pre-validations
    as the legacy /mode endpoint. Disabling live is unrestricted (it makes
    the bot SAFER). Paper is always toggleable.

    Refuses to leave the empty set — if you want the bot to stop trading
    entirely, use the kill switch.
    """
    from polybot.config import settings as cfg
    import hashlib
    import hmac as _hmac

    current = await enabled_modes()
    target = set(current)
    if body.paper is True:
        target.add("paper")
    elif body.paper is False:
        target.discard("paper")
    if body.live is True:
        target.add("live")
    elif body.live is False:
        target.discard("live")

    if not target:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "must leave at least one mode enabled — use the kill switch to pause trading",
        )

    # Only ADDING live (transition from no-live → live) needs the HMAC.
    # Removing live is always allowed (de-risking the bot).
    if "live" in target and "live" not in current:
        if not x_live_confirm:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "enabling live requires X-Live-Confirm")
        parts = x_live_confirm.split(":", 1)
        if len(parts) != 2:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "bad live confirm format")
        try:
            ts = int(parts[0])
        except ValueError:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "bad live confirm timestamp") from None
        if abs(int(time.time()) - ts) > _LIVE_SWITCH_SKEW_S:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "live confirm expired")
        secret = cfg.admin_token.get_secret_value().encode()
        expected = _hmac.new(secret, f"switch-to-live:{ts}".encode(), hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(parts[1], expected):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "bad live confirm hmac")
        await _validate_live_ready()

    new_modes = await set_enabled_modes(target, actor=f"admin@{_client_ip(request)}")
    return {"enabled_modes": sorted(new_modes), "mode": await current_mode()}


@router.get("/mode/live-challenge", dependencies=[Depends(require_admin)])
async def get_live_challenge() -> dict[str, str]:
    """Server-issued live-confirm token for the dashboard's mode-switch flow.

    The ModeTab used to require the operator to copy-paste a
    `docker compose exec api python -c "..."` command that printed an
    HMAC string. That was the worst UX in the app — and an obvious
    target for shoulder-surfing. The endpoint is `require_admin`-gated
    so only the same user already authenticated to set mode can fetch
    it. The token is short-lived (60 s skew window enforced in
    `set_mode_endpoint`).
    """
    from polybot.config import settings as cfg
    import hashlib
    import hmac as _hmac

    ts = int(time.time())
    secret = cfg.admin_token.get_secret_value().encode()
    sig = _hmac.new(secret, f"switch-to-live:{ts}".encode(), hashlib.sha256).hexdigest()
    return {"confirm_token": f"{ts}:{sig}", "epoch": str(ts)}


class ModeSwitch(BaseModel):
    mode: str = Field(pattern="^(paper|live)$")


@router.post("/mode", dependencies=[Depends(require_admin)])
async def set_mode_endpoint(
    body: ModeSwitch, request: Request,
    x_live_confirm: str | None = Header(default=None),
) -> dict[str, Any]:
    """Switch trading mode. Live-mode flips require an additional HMAC
    in `X-Live-Confirm: <hmac(secret, 'switch-to-live:{epoch}')>` within
    a 60 s skew window — a leaked admin token alone cannot flip the bot
    to live mode.
    """
    from polybot.config import settings as cfg
    import hashlib
    import hmac as _hmac

    if body.mode == "live":
        if not x_live_confirm:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "live switch requires X-Live-Confirm")
        # parse "epoch:hmac"
        parts = x_live_confirm.split(":", 1)
        if len(parts) != 2:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "bad live confirm format")
        try:
            ts = int(parts[0])
        except ValueError:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "bad live confirm timestamp") from None
        if abs(int(time.time()) - ts) > _LIVE_SWITCH_SKEW_S:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "live confirm expired")
        secret = cfg.admin_token.get_secret_value().encode()
        expected = _hmac.new(secret, f"switch-to-live:{ts}".encode(), hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(parts[1], expected):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "bad live confirm hmac")

        # Live-readiness pre-validations — fail FAST so the operator
        # doesn't switch to live, watch nothing happen, then have to dig
        # through executor logs to find out why orders aren't flowing.
        # Each check raises with a precise remediation so the dashboard
        # can surface "go fix X first" instead of just 4xx.
        await _validate_live_ready()

    old = await current_mode()
    await set_mode(body.mode, actor=f"admin@{_client_ip(request)}")
    return {"mode": body.mode, "previous": old}


async def _validate_live_ready() -> None:
    """Block paper→live switch unless the bot is actually able to trade.

    Three pre-checks:
      1. Kill switch must NOT already be active (otherwise every order
         would be rejected at preflight and the operator wouldn't know).
      2. An active wallet credential must exist in `wallet_credentials`
         OR a POLYMARKET_PRIVATE_KEY + funder address in .env (legacy
         fallback). Without either, the executor silently no-ops every
         signal.
      3. Best-effort: surface a warning if WALLET_ENCRYPTION_KEY is
         missing — credentials in DB couldn't be decrypted anyway.

    USDC.e allowance is NOT pre-checked here (web3 round-trip is slow +
    needs an RPC URL we don't depend on yet). The first order will still
    fail cleanly with reason='allowance_missing' which surfaces the bad
    state — documented in BUGS.md.
    """
    from polybot.config import settings as _settings
    from polybot.db import session_scope
    from polybot.models import WalletCredential
    from polybot.redis_bus import kill_status
    from sqlalchemy import func, select

    # 1. Kill switch
    ks = await kill_status()
    if ks:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"kill_switch is active ({ks!r}) — clear it via POST /admin/kill/clear before switching to live",
        )

    # 2. Signing credential
    async with session_scope() as s:
        n_active = (await s.execute(
            select(func.count(WalletCredential.id))
            .where(WalletCredential.is_active.is_(True))
        )).scalar_one()
    has_env_fallback = bool(
        _settings.polymarket_private_key and _settings.polymarket_funder_address
    )
    if n_active == 0 and not has_env_fallback:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no active wallet credential — add one via POST /admin/settings/wallet, "
            "or set POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER_ADDRESS in .env",
        )

    # 3. WALLET_ENCRYPTION_KEY must be present if relying on DB creds
    if n_active > 0 and _settings.wallet_encryption_key is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "WALLET_ENCRYPTION_KEY env var is unset but DB has encrypted "
            "wallets — they cannot be decrypted. Set the key in .env first.",
        )


# ---- Risk ----------------------------------------------------------------


@router.patch("/risk", dependencies=[Depends(require_admin)])
async def patch_risk(patch: dict[str, Any], request: Request) -> dict[str, Any]:
    """Shallow-merge `patch` into the per-mode risk overrides.

    Example body: {"position": {"max_open_positions": 75}}
    """
    new = await set_overrides("risk", patch, actor=f"admin@{_client_ip(request)}")
    return {"overrides": new, "effective": await merged_risk()}


@router.delete("/risk", dependencies=[Depends(require_admin)])
async def delete_risk_overrides(request: Request) -> dict[str, Any]:
    await clear_overrides("risk", actor=f"admin@{_client_ip(request)}")
    return {"overrides": {}, "effective": await merged_risk()}


# ---- Categories ----------------------------------------------------------


class CategoryUpsert(BaseModel):
    name: str
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    top_n: int = Field(default=30, ge=1, le=500)
    min_win_rate: float = Field(default=0.55, ge=0.0, le=1.0)


@router.patch("/categories/{name}", dependencies=[Depends(require_admin)])
async def patch_category(name: str, patch: dict[str, Any], request: Request) -> dict[str, Any]:
    """Partial override of one category. Body shape: {enabled?, top_n?,
    min_win_rate?, tags?}."""
    allowed = {"enabled", "top_n", "min_win_rate", "tags"}
    bad = set(patch) - allowed
    if bad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown fields: {sorted(bad)}")
    current = await get_overrides("categories")
    cat = current.get(name, {})
    cat = {**cat, **patch}
    new = await set_overrides("categories", {name: cat}, actor=f"admin@{_client_ip(request)}")
    return {"name": name, "overrides": new.get(name), "effective": (await merged_categories()).get(name)}


@router.post("/categories", dependencies=[Depends(require_admin)])
async def add_category(body: CategoryUpsert, request: Request) -> dict[str, Any]:
    """Add a NEW category at runtime. Tags are case-folded on the Polymarket
    side, so this is fully equivalent to editing categories.yaml + waiting
    for the 5-min TTL — without the wait."""
    if not body.tags:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "tags is required for new categories")
    new = await set_overrides(
        "categories",
        {body.name: {
            "tags": body.tags, "enabled": body.enabled,
            "top_n": body.top_n, "min_win_rate": body.min_win_rate,
        }},
        actor=f"admin@{_client_ip(request)}",
    )
    return {"name": body.name, "effective": (await merged_categories()).get(body.name)}


@router.delete("/categories/{name}", dependencies=[Depends(require_admin)])
async def disable_category(name: str, request: Request) -> dict[str, Any]:
    """Soft-disable a category — sets enabled=false in override. We never
    hard-delete a category from overrides because that risks reverting to
    a yaml-baseline-enabled state silently."""
    current = await get_overrides("categories")
    cat = current.get(name, {})
    cat["enabled"] = False
    await set_overrides("categories", {name: cat}, actor=f"admin@{_client_ip(request)}")
    return {"name": name, "enabled": False}


# ---- Gates ---------------------------------------------------------------


@router.patch("/gates", dependencies=[Depends(require_admin)])
async def patch_gates(patch: dict[str, Any], request: Request) -> dict[str, Any]:
    """Shallow-merge `patch` into per-mode gates overrides.

    Body shape mirrors gates.yaml top-level (we don't validate gate names
    here — the gate chain ignores unknown keys at startup).
    """
    new = await set_overrides("gates", patch, actor=f"admin@{_client_ip(request)}")
    return {"overrides": new, "effective": await merged_gates()}


# ---- Wallet ---------------------------------------------------------------


class WalletCreate(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    address: str = Field(pattern="^0x[a-fA-F0-9]{40}$")
    funder_address: str = Field(pattern="^0x[a-fA-F0-9]{40}$")
    signature_type: int = Field(default=1, ge=0, le=2)
    private_key: str = Field(min_length=64, max_length=130)


@router.get("/wallet", dependencies=[Depends(require_admin)])
async def list_wallets(s: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = (await s.execute(
        select(WalletCredential).order_by(WalletCredential.created_at.desc())
    )).scalars().all()
    return [
        {
            "id": r.id, "label": r.label, "address": r.address,
            "funder_address": r.funder_address, "signature_type": r.signature_type,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
        }
        for r in rows
    ]


@router.post(
    "/wallet",
    dependencies=[Depends(require_admin)],
    status_code=status.HTTP_201_CREATED,
)
async def add_wallet(body: WalletCreate, request: Request) -> dict[str, Any]:
    """Encrypt + insert a new wallet. Private key is AAD-bound to its
    address so a stolen ciphertext row can't be decrypted into a
    different address slot. The response NEVER echoes the key.

    CRITICAL: must use `session_scope()` (which commits on success) — the
    legacy `get_session` FastAPI dependency only yields and never commits,
    so flushed-but-not-committed writes silently rolled back. Every
    dashboard "Add wallet" click was a data loss until this fix.
    """
    pk = body.private_key.strip()
    # Normalise to bytes — most operators paste a 0x-prefixed hex string.
    if pk.startswith("0x"):
        pk = pk[2:]
    try:
        pk_bytes = bytes.fromhex(pk)
    except ValueError:
        # Maybe they pasted utf-8 raw — accept as-is.
        pk_bytes = body.private_key.strip().encode("utf-8")
    aad = f"wallet:{body.address.lower()}:signing".encode()
    ciphertext = encrypt(pk_bytes, aad=aad)
    actor = f"admin@{_client_ip(request)}"

    async with session_scope() as s:
        # Deactivate previous active wallet so there's always exactly one signer.
        await s.execute(
            update(WalletCredential)
            .where(WalletCredential.is_active.is_(True))
            .values(is_active=False)
        )
        new = WalletCredential(
            label=body.label, address=body.address.lower(),
            funder_address=body.funder_address.lower(),
            signature_type=body.signature_type,
            encrypted_private_key=ciphertext,
            is_active=True,
            created_at=datetime.now(tz=timezone.utc),
        )
        s.add(new)
        # Audit log — every wallet credential add must be reconstructable
        # post-incident. Never log the plaintext key (it's already wiped
        # from the request body at this point).
        s.add(AuditLog(
            actor=actor,
            event="wallet_credential_added",
            payload={
                "address": new.address, "funder_address": new.funder_address,
                "label": new.label, "signature_type": new.signature_type,
            },
        ))
        await s.flush()
        wallet_id = new.id
        wallet_addr = new.address
        wallet_funder = new.funder_address
    log.info(
        "wallet_credential_added",
        wallet_id=wallet_id, address=wallet_addr, label=body.label, actor=actor,
    )
    return {
        "id": wallet_id, "address": wallet_addr, "funder_address": wallet_funder,
        "label": body.label, "is_active": True,
    }


@router.delete("/wallet/{wallet_id}", dependencies=[Depends(require_admin)])
async def soft_delete_wallet(wallet_id: int, request: Request) -> dict[str, Any]:
    """Soft-disable a wallet credential. Same commit-fix as add_wallet —
    moved off get_session() so the soft-delete actually persists."""
    actor = f"admin@{_client_ip(request)}"
    async with session_scope() as s:
        row = (await s.execute(
            select(WalletCredential).where(WalletCredential.id == wallet_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "wallet not found")
        prev_active = row.is_active
        row.is_active = False
        s.add(AuditLog(
            actor=actor,
            event="wallet_credential_disabled",
            payload={"wallet_id": wallet_id, "address": row.address,
                     "was_active": prev_active},
        ))
    log.info(
        "wallet_credential_disabled",
        wallet_id=wallet_id, actor=actor,
    )
    return {"id": wallet_id, "is_active": False}
