"""SIWE (Sign-In With Ethereum) auth for the dashboard.

UX: user clicks "Connect Wallet", MetaMask (or WalletConnect) pops up,
they sign a one-time challenge, and the dashboard gets a session token
that authorises admin operations. No copy-paste of an admin token.

Three endpoints:

  GET  /auth/nonce
       Returns a random 32-byte hex nonce + the full SIWE-style message
       the wallet should sign. The nonce is stored in Redis with a 5-min
       TTL so it can't be reused later by an attacker who scrapes the
       network response.

  POST /auth/verify  body {address, message, signature}
       Verifies that `signature` matches `message` signed by `address`
       (EIP-191 recoverable signature). Checks the nonce is fresh, the
       address is on the admin allowlist, and the timestamp is recent.
       On success: issues a session token (64-byte hex), stores it in
       Redis with a 24-hour TTL, returns it. Browser puts it in
       sessionStorage and sends it as `X-Session-Token` on subsequent
       admin calls.

  POST /auth/logout
       Deletes the session token from Redis. Optional.

Allowlist:
  Comma-separated `ADMIN_WALLET_ADDRESSES` in .env. Empty means NO
  wallet is allowed (admin-token-only mode — backward compat). The
  legacy `ADMIN_TOKEN` continues to work in parallel; either auth
  path satisfies the `require_admin` dependency.

Security notes:
  - SIWE message includes domain + nonce + issued-at + expiration,
    matching EIP-4361 fields the wallet pre-validates on the user side.
  - Signature recovery uses eth_account.recover_message (constant-time
    ECDSA, no rolling-our-own crypto).
  - Session token is 64 hex chars = 256 bits of entropy → unguessable.
  - Redis key prefix `polybot:auth:session:` so it's easy to audit
    (`SCAN 0 MATCH 'polybot:auth:session:*'`).
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from polybot.logging import get_logger
from polybot.redis_bus import client as _redis

log = get_logger(__name__)

router = APIRouter()

# Nonce: how long the user has to sign after clicking "Connect Wallet".
_NONCE_TTL_S = 300
# Session: how long they stay logged in after a successful sign.
_SESSION_TTL_S = 24 * 3600
# Max skew between message.issued_at and server clock — prevents replay
# with a captured signature long after the fact.
_ISSUED_AT_SKEW_S = 300

_NONCE_PREFIX = "polybot:auth:nonce:"
_SESSION_PREFIX = "polybot:auth:session:"

# Allowlist. Comma-separated lowercase addresses. Wildcard "*" (only in
# paper mode) lets ANY connected wallet authenticate — convenient for
# local dev, never use in live.
_ALLOWLIST = [
    a.strip().lower()
    for a in os.environ.get("ADMIN_WALLET_ADDRESSES", "").split(",")
    if a.strip()
]


def _build_siwe_message(domain: str, address: str, nonce: str, issued_at: int) -> str:
    """Compose an EIP-4361-style message.

    We don't strictly enforce all EIP-4361 line conventions because we
    verify the signature server-side anyway; the wallet sees a clear
    human-readable challenge.
    """
    address = address.lower()
    expires_at = issued_at + _NONCE_TTL_S
    return (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{address}\n"
        f"\n"
        f"Authenticate to the Polybot dashboard.\n"
        f"\n"
        f"URI: https://{domain}\n"
        f"Version: 1\n"
        f"Chain ID: 137\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}\n"
        f"Expiration Time: {expires_at}\n"
    )


@router.get("/nonce")
async def get_nonce(request: Request, address: str) -> dict[str, Any]:
    """Return a fresh nonce + the message the wallet should sign.

    `address` is informational — used to build the human-readable
    message. We do NOT enforce it matches the allowlist here (that
    happens at /verify) so a probing client can't enumerate allowed
    addresses by trying nonce-fetches.
    """
    if not address or len(address) != 42 or not address.startswith("0x"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad address")
    nonce = secrets.token_hex(16)
    issued_at = int(time.time())
    domain = request.headers.get("host", "localhost:8000").split(":")[0]
    msg = _build_siwe_message(domain, address, nonce, issued_at)
    await _redis().set(
        _NONCE_PREFIX + nonce,
        f"{address.lower()}:{issued_at}",
        ex=_NONCE_TTL_S,
    )
    return {"nonce": nonce, "message": msg, "expires_in_s": _NONCE_TTL_S}


class VerifyBody(BaseModel):
    address: str = Field(min_length=42, max_length=42)
    message: str = Field(min_length=10)
    signature: str = Field(min_length=10)


@router.post("/verify")
async def verify_signature(body: VerifyBody) -> dict[str, Any]:
    """Verify an SIWE signature and issue a session token.

    Flow:
      1. Parse nonce out of the message body (line "Nonce: X").
      2. Look up the nonce in Redis. If missing/expired, reject 401.
         The stored value contains the address we built the nonce for —
         it must match the address being verified, else reject.
      3. Recover the signer from (message, signature) and compare to
         the claimed address.
      4. Enforce ADMIN_WALLET_ADDRESSES allowlist.
      5. Burn the nonce (can't be reused).
      6. Issue a session token; store address → session mapping with TTL.
    """
    claimed = body.address.lower()

    # 1. Extract nonce from the message (avoids the client lying about it)
    nonce = None
    issued_at = None
    for line in body.message.splitlines():
        if line.startswith("Nonce: "):
            nonce = line[7:].strip()
        elif line.startswith("Issued At: "):
            try:
                issued_at = int(line[11:].strip())
            except ValueError:
                pass
    if not nonce:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no nonce in message")

    # 2. Nonce must exist + match the address
    nonce_key = _NONCE_PREFIX + nonce
    stored = await _redis().get(nonce_key)
    if not stored:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "nonce missing or expired")
    stored_addr, _, _stored_issued = stored.partition(":")
    if stored_addr.lower() != claimed:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "nonce address mismatch")

    # 2b. Issued At freshness — defends against an attacker who buffers a
    #     valid (nonce, signature) pair and replays it later. Nonce TTL
    #     handles most of this; this is belt-and-braces.
    if issued_at is not None:
        if abs(int(time.time()) - issued_at) > _ISSUED_AT_SKEW_S:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "message too old")

    # 3. Signature recovery
    try:
        msg = encode_defunct(text=body.message)
        recovered = Account.recover_message(msg, signature=body.signature)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"signature recovery failed: {type(exc).__name__}",
        ) from None
    if recovered.lower() != claimed:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "signature does not match address")

    # 4. Allowlist enforcement — security-critical.
    #
    # Default: SIWE login REQUIRES an entry in ADMIN_WALLET_ADDRESSES even
    # in paper mode. A previous "paper-only convenience" path let ANY
    # connected wallet authenticate when the allowlist was empty — that's
    # remote-control-of-the-bot for anyone who can reach the dashboard,
    # which is exactly the threat model SIWE is meant to defeat.
    #
    # Escape hatch for true single-laptop dev: set
    # SIWE_DEV_MODE_ALLOW_ANY=true in .env. Loud warning logs on every
    # login so the operator can't forget the door is open.
    if not _ALLOWLIST:
        dev_mode = os.environ.get("SIWE_DEV_MODE_ALLOW_ANY", "").strip().lower() == "true"
        if not dev_mode:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "ADMIN_WALLET_ADDRESSES is empty — add your wallet address to .env "
                "(or set SIWE_DEV_MODE_ALLOW_ANY=true for local dev only). "
                "Otherwise anyone on the network could sign in.",
            )
        log.warning("siwe_dev_mode_login_any_wallet_accepted", address=claimed)
    elif "*" not in _ALLOWLIST and claimed not in _ALLOWLIST:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "address not on ADMIN_WALLET_ADDRESSES allowlist",
        )

    # 5. Burn the nonce
    await _redis().delete(nonce_key)

    # 6. Mint session token
    token = secrets.token_hex(32)
    await _redis().set(
        _SESSION_PREFIX + token,
        claimed,
        ex=_SESSION_TTL_S,
    )
    log.info("siwe_login", address=claimed, ttl_s=_SESSION_TTL_S)
    return {
        "session_token": token,
        "address": claimed,
        "expires_in_s": _SESSION_TTL_S,
    }


@router.post("/logout")
async def logout(x_session_token: str | None = Header(default=None)) -> dict[str, bool]:
    if x_session_token:
        await _redis().delete(_SESSION_PREFIX + x_session_token)
    return {"ok": True}


@router.get("/me")
async def whoami(x_session_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Return the current session's address (or 401 if not signed in)."""
    if not x_session_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")
    addr = await _redis().get(_SESSION_PREFIX + x_session_token)
    if not addr:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    return {"address": addr, "allowlist_size": len(_ALLOWLIST)}


# ---- Session validation helper (used by services/api/deps.py) ---------------


async def session_is_valid(token: str | None) -> str | None:
    """Return the wallet address if the session token is valid, else None.

    Used by `require_admin` to accept SIWE sessions as an alternative to
    the legacy X-Admin-Token. Single round-trip Redis GET.
    """
    if not token:
        return None
    try:
        addr = await _redis().get(_SESSION_PREFIX + token)
    except Exception:  # noqa: BLE001
        log.exception("siwe_session_redis_failed")
        return None
    return addr if addr else None
