"""Add markets.outcomes JSONB for multi-outcome (non-YES/NO) markets.

Without this column we cannot map an arbitrary outcome string (e.g.
"TYLOO", "LYNN VISION", "EMILIO NAVA") back to the correct CLOB token,
because `yes_token_id` / `no_token_id` are positional aliases that
correspond to outcomes[0] / outcomes[1] respectively. The bug was that
non-binary outcomes always fell through to `yes_token_id`, yielding
the WRONG side's mark price (= opponent's price), which inflated some
mark-to-market PnL displays and deflated others by 100%.

Revision ID: 0003_market_outcomes
Revises: 0002_realised_stats_and_widening
Create Date: 2026-06-05

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_market_outcomes"
down_revision = "0002_realised_stats_and_widening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ADD COLUMN IF NOT EXISTS via raw SQL — Alembic's add_column has no
    # idempotency flag, but we want to be safe re-running this migration
    # on a DB where someone manually applied it.
    op.execute(
        "ALTER TABLE markets ADD COLUMN IF NOT EXISTS outcomes JSONB"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE markets DROP COLUMN IF EXISTS outcomes")
