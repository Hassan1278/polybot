"""Add markets.event_id for the one-position-per-event guard.

Polymarket groups sibling markets under a parent *event* (every candidate
market in one election, both sides of one match, etc.). The Gamma payload
already carries this under ``events[]`` — we previously kept only the tags off
it. Storing the event id lets the risk preflight enforce "one position per
event" so the bot can't take multiple, often offsetting, positions on the same
underlying event (e.g. NO on two frontrunners in the same primary).

Nullable: markets with no parent event (or rows not yet re-ingested) stay NULL
and the guard simply skips them. The bulk market_ingest upserts all active
markets every cycle, so active markets self-populate within one pass — no
separate backfill is required.

Revision ID: 0009_market_event_id
Revises: 0008_fill_signal_mode_unique
Create Date: 2026-06-19

"""

from __future__ import annotations

from alembic import op

revision = "0009_market_event_id"
down_revision = "0008_fill_signal_mode_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE markets ADD COLUMN IF NOT EXISTS event_id VARCHAR(80)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_markets_event_id ON markets (event_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_markets_event_id")
    op.execute("ALTER TABLE markets DROP COLUMN IF EXISTS event_id")
