"""AES-256-GCM encryption helpers for at-rest secrets (wallet keys).

Design choices:
  - **AEAD** (AES-GCM): authenticated encryption — any tampering with the
    ciphertext is detected on decrypt, not silently passed through.
  - **Master key**: 32 random bytes, base64-encoded in
    `settings.wallet_encryption_key`. NEVER persisted to DB; backup
    procedure is on the operator (e.g. via GitHub-backups of `.env`).
  - **Per-encryption nonce**: 12 random bytes (NIST recommended for GCM),
    prepended to ciphertext. Each call uses a fresh nonce — nonce reuse
    under the same key catastrophically breaks GCM.
  - **AAD binding**: optional additional authenticated data binds the
    ciphertext to a specific use, e.g. `aad=b"wallet:0xabc:signing"`.
    Decrypt with mismatched AAD fails. This prevents cross-context
    ciphertext replay (someone stealing a row from a different table).
  - **Wire format**: `nonce(12) || ciphertext(N) || tag(16)`, all bytes,
    stored as bytea in PostgreSQL.

Key rotation:
  1. Generate a new key, set `WALLET_ENCRYPTION_KEY_OLD=<current>` and
     `WALLET_ENCRYPTION_KEY=<new>` in .env.
  2. Run `python -m scripts.rotate_wallet_keys` (not in this commit; add
     when first rotation is needed).
  3. After successful re-encrypt, drop `WALLET_ENCRYPTION_KEY_OLD`.

Loss of `WALLET_ENCRYPTION_KEY` is unrecoverable — encrypted private keys
become permanently inaccessible. Document this in setup docs.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from polybot.config import settings

_NONCE_BYTES = 12
_KEY_BYTES = 32


def _load_key() -> bytes:
    """Resolve the active master key from settings. Validates length +
    entropy at the boundary so misconfiguration surfaces early, not at
    first decrypt-time."""
    raw = settings.wallet_encryption_key
    if raw is None:
        raise RuntimeError(
            "WALLET_ENCRYPTION_KEY is not set. Generate one: "
            "python -c 'import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())'"
        )
    secret_value = raw.get_secret_value() if hasattr(raw, "get_secret_value") else raw
    try:
        decoded = base64.b64decode(secret_value, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("WALLET_ENCRYPTION_KEY must be base64") from exc
    if len(decoded) != _KEY_BYTES:
        raise RuntimeError(
            f"WALLET_ENCRYPTION_KEY must decode to exactly {_KEY_BYTES} bytes "
            f"(got {len(decoded)})"
        )
    # Reject obviously-weak keys (all-zero, all-FF). Not a substitute for
    # real entropy testing — just catches the most common operator mistakes.
    if decoded == b"\x00" * _KEY_BYTES or decoded == b"\xff" * _KEY_BYTES:
        raise RuntimeError("WALLET_ENCRYPTION_KEY is a forbidden weak value")
    return decoded


def encrypt(plaintext: bytes | str, *, aad: bytes | None = None) -> bytes:
    """Encrypt `plaintext` and return `nonce || ciphertext || tag` bytes.

    `aad` (optional) binds the ciphertext to a context (e.g. wallet
    address). Decrypt MUST pass the same `aad` or fails with InvalidTag.
    """
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    nonce = os.urandom(_NONCE_BYTES)
    aead = AESGCM(_load_key())
    ct = aead.encrypt(nonce, plaintext, aad)
    return nonce + ct


def decrypt(blob: bytes, *, aad: bytes | None = None) -> bytes:
    """Inverse of :func:`encrypt`. Raises RuntimeError on any tamper /
    wrong-AAD / wrong-key — callers should NOT distinguish the cause to
    avoid leaking information via error messages."""
    if not isinstance(blob, bytes) or len(blob) < _NONCE_BYTES + 16:
        raise RuntimeError("ciphertext blob too short or wrong type")
    nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    aead = AESGCM(_load_key())
    try:
        return aead.decrypt(nonce, ct, aad)
    except InvalidTag as exc:
        raise RuntimeError("decryption failed (tag/AAD/key mismatch)") from exc


def generate_master_key() -> str:
    """Convenience for first-run setup: emit a base64-encoded 32-byte key.
    Not called automatically — operator runs `python -c "from polybot.crypto
    import generate_master_key; print(generate_master_key())"` and pastes
    into .env."""
    return base64.b64encode(os.urandom(_KEY_BYTES)).decode("ascii")
