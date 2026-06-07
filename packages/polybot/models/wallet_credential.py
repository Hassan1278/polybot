"""Bot-controlled wallet credentials, encrypted at rest.

Distinct from the `wallets` table (which tracks smart-money wallets we
MIRROR). This table stores the credentials of OUR signing wallet(s) for
live-mode order placement.

Security model:
  - `encrypted_private_key` is AES-256-GCM ciphertext from
    `polybot.crypto.encrypt(key, aad=f'wallet:{address}'.encode())`.
  - Plain `address` and `funder_address` are NOT secrets (they're on-chain
    public info) so they're stored cleartext for join/lookup.
  - `WALLET_ENCRYPTION_KEY` must be set in .env or the column is
    undecryptable. Backup the env (we have GitHub-backups) — losing the
    key bricks the wallet.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from polybot.db import Base


class WalletCredential(Base):
    __tablename__ = "wallet_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # human-readable label shown in the dashboard
    label: Mapped[str] = mapped_column(String(128))
    # the L2 address that signs orders (= our public key)
    address: Mapped[str] = mapped_column(String(42), unique=True, index=True)
    # Polymarket proxy/funder address (sometimes equal to `address` for EOA,
    # different when using email/magic-link account abstraction).
    funder_address: Mapped[str] = mapped_column(String(42))
    # 0=EOA, 1=email/magic, 2=browser  (matches polymarket_signature_type env)
    signature_type: Mapped[int] = mapped_column(Integer, default=1)
    # AES-256-GCM ciphertext of the raw hex/utf-8 private key. Always > 28
    # bytes (12 nonce + 16 tag minimum). PG bytea column.
    encrypted_private_key: Mapped[bytes] = mapped_column(LargeBinary)
    # Only one credential can be "active" at a time (enforced by app, not
    # DB — we want operator visibility into which one signed when).
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
