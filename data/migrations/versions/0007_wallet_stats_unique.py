"""WalletStats unique constraint on (address, window).

Before this migration the inserts at services/signals/stats_loop.py and
services/ingest/jobs/leaderboard_scraper.py blindly INSERTed a fresh row
each cycle. The wallet_quality gate's SELECT then averaged ALL historical
snapshots per wallet — including week-old ones — instead of the latest.
With stats_loop running every 5 min, a wallet would accumulate ~280
rows/day, and avg(win_rate) silently regressed toward the historical mean
rather than reflecting current skill.

Forward fix:
  1. Dedup current `wallet_stats` keeping the latest `computed_at` per
     (address, window). Older rows are dropped — they were never
     authoritative once the most recent one existed.
  2. Add UNIQUE(address, window).
  3. Application code switches to `pg_insert.on_conflict_do_update`.

Revision ID: 0007_wallet_stats_unique
Revises: 0006_wallet_credentials
Create Date: 2026-06-17

"""

from __future__ import annotations

from alembic import op

revision = "0007_wallet_stats_unique"
down_revision = "0006_wallet_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Dedup: keep the latest row per (address, window).
    #    The "obvious" DELETE … USING self-join scans O(N²) on a table
    #    without a covering index — 370k rows took 10+ minutes in prod.
    #    DISTINCT ON via a swap-table is O(N log N) and finishes in
    #    seconds. We materialise the keep-set, truncate, refill.
    op.execute(
        """
        CREATE TEMP TABLE _wallet_stats_keep AS
        SELECT DISTINCT ON (address, "window") *
        FROM wallet_stats
        ORDER BY address, "window", computed_at DESC, id DESC
        """
    )
    op.execute("TRUNCATE wallet_stats")
    op.execute("INSERT INTO wallet_stats SELECT * FROM _wallet_stats_keep")
    op.execute("DROP TABLE _wallet_stats_keep")
    # 2. Promote the existing non-unique index to UNIQUE. Drop + recreate
    #    so any name collision is resolved cleanly.
    op.drop_index("ix_wallet_stats_addr_window", table_name="wallet_stats")
    op.create_unique_constraint(
        "uq_wallet_stats_addr_window",
        "wallet_stats",
        ["address", "window"],
    )
    # 3. Keep a btree index for SELECT scans — UNIQUE constraint covers
    #    equality lookup but ORDER BY computed_at filtering is common.
    op.create_index(
        "ix_wallet_stats_addr_window",
        "wallet_stats",
        ["address", "window", "computed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_stats_addr_window", table_name="wallet_stats")
    op.drop_constraint(
        "uq_wallet_stats_addr_window", "wallet_stats", type_="unique",
    )
    op.create_index(
        "ix_wallet_stats_addr_window",
        "wallet_stats",
        ["address", "window"],
    )
