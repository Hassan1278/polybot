"""Switch fills idempotency index to composite (signal_id, mode).

Migration 0004 created `uq_fills_signal_id ON fills (signal_id) WHERE
signal_id IS NOT NULL` for single-mode operation: one signal → one fill.

The parallel paper+live mode (introduced 2026-06-17) needs each signal
to produce up to ONE fill PER MODE — paper fill + live fill — so the
old single-column uniqueness violates the data schema. Without this
fix the live-mode iteration's INSERT raises IntegrityError, the
exception propagates out of handle(), and the signal gets DLQ'd. On
redelivery the (mode-blind) dedup pre-check sees the paper fill and
skips the entire signal — live never fires.

Switch to composite UNIQUE (signal_id, mode) WHERE signal_id IS NOT NULL.
Still guarantees idempotency per (signal_id, mode) — a redelivered
signal cannot double-insert the paper-side or the live-side — while
allowing one row per mode.

Revision ID: 0008_fill_signal_mode_unique
Revises: 0007_wallet_stats_unique
Create Date: 2026-06-17

"""

from __future__ import annotations

from alembic import op

revision = "0008_fill_signal_mode_unique"
down_revision = "0007_wallet_stats_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the single-column unique index from 0004.
    op.execute("DROP INDEX IF EXISTS uq_fills_signal_id")
    # No pre-flight dedup needed: existing fills are mode='paper' (we've been
    # paper-only). They naturally satisfy the new composite uniqueness.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_fills_signal_id_mode
        ON fills (signal_id, mode)
        WHERE signal_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_fills_signal_id_mode")
    # Recreate the 0004 single-column index. If multi-mode rows exist,
    # this will fail — that's correct: you cannot downgrade past 0008
    # while parallel-mode data is present.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_fills_signal_id
        ON fills (signal_id)
        WHERE signal_id IS NOT NULL
        """
    )
