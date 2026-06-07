"""Add partial UNIQUE index on fills.signal_id (executor idempotency).

A single signal must produce AT MOST one fill row. Without this constraint,
manual signal replay or Redis pub/sub re-delivery during a subscriber crash
could write multiple Fill rows for the same signal_id, corrupting position
accounting.

The index is PARTIAL (`WHERE signal_id IS NOT NULL`) because SETTLE rows
(written by settle_resolved_markets in pnl_loop) carry signal_id=NULL —
those are not signal-driven and should not be uniqueness-checked.

Pre-flight cleanup: if any existing rows already violate uniqueness (legacy
duplicates from before this fix), we DELETE the higher-id row (= the later
duplicate) so the index can be created without an error.

Revision ID: 0004_fill_signal_unique
Revises: 0003_market_outcomes
Create Date: 2026-06-07

"""

from __future__ import annotations

from alembic import op

revision = "0004_fill_signal_unique"
down_revision = "0003_market_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop any pre-existing duplicates (keep the EARLIEST row per signal_id).
    # The COALESCE-on-id prefers the earliest because lower id = earlier insert.
    op.execute("""
        DELETE FROM fills a
        USING fills b
        WHERE a.signal_id IS NOT NULL
          AND a.signal_id = b.signal_id
          AND a.id > b.id
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_fills_signal_id
        ON fills (signal_id)
        WHERE signal_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_fills_signal_id")
