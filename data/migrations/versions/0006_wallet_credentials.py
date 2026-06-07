"""Bot-controlled wallet credentials, encrypted at rest.

Stores the SIGNING wallet (our private key, encrypted via AES-256-GCM)
that the live executor uses to place orders on Polymarket. Distinct
from the `wallets` table (which is the smart-money roster we MIRROR).

`encrypted_private_key` is bytea: nonce(12) || ciphertext(N) || tag(16),
produced by `polybot.crypto.encrypt(key, aad=f'wallet:{addr}'.encode())`.
Decryption requires `WALLET_ENCRYPTION_KEY` env var.

Revision ID: 0006_wallet_credentials
Revises: 0005_trades_retention
Create Date: 2026-06-07

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_wallet_credentials"
down_revision = "0005_trades_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_credentials",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("address", sa.String(42), nullable=False, unique=True),
        sa.Column("funder_address", sa.String(42), nullable=False),
        sa.Column("signature_type", sa.Integer, nullable=False, server_default="1"),
        sa.Column("encrypted_private_key", sa.LargeBinary, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_wallet_credentials_is_active",
        "wallet_credentials",
        ["is_active"],
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_credentials_is_active", table_name="wallet_credentials")
    op.drop_table("wallet_credentials")
