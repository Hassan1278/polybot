"""TimescaleDB hypertable conversion + retention policy for `trades`.

Three-step migration. The current `trades` table is a normal Postgres table
(the original `create_hypertable` call in 0001 either ran on an instance
without the timescaledb extension active, or its data migration option was
off — net result: `trades` is a heap table with growing indexes and no
chunking).

Step 1: Make the partition column (`ts`) part of every UNIQUE constraint.
        Timescale REQUIRES this; without it `create_hypertable` errors out
        with "cannot create a unique index without the column ts (used in
        partitioning)".
        - Drop the single-column PK `(id)` and recreate as composite
          `(id, ts)`. App code uses `id` for FK references only when
          inserting rows it knows the ts of (always — it's NOT NULL),
          so the composite is API-compatible.
        - Recreate the partial unique index on `tx_hash` as composite
          `(tx_hash, ts)`.
Step 2: Convert to hypertable with 7-day chunks. `migrate_data => true`
        repacks existing rows online (brief ACCESS EXCLUSIVE lock per
        chunk, completes in seconds at our row count).
Step 3: Add 180-day retention policy.

If anything is already in place (fresh install with hypertable, prior
manual conversion), the `if_not_exists => TRUE` flags make each step a
no-op.

Revision ID: 0005_trades_retention
Revises: 0004_fill_signal_unique
Create Date: 2026-06-07

"""

from __future__ import annotations

from alembic import op

revision = "0005_trades_retention"
down_revision = "0004_fill_signal_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1 — adjust unique constraints so partition column `ts` is included.
    # Conditional drop+recreate so the migration is safe on a hypertable
    # that's already correctly configured.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'trades'::regclass AND conname = 'trades_pkey'
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_attribute a JOIN pg_constraint c ON a.attrelid=c.conrelid
                WHERE c.conname = 'trades_pkey' AND a.attname = 'ts'
                  AND a.attnum = ANY(c.conkey)
            ) THEN
                ALTER TABLE trades DROP CONSTRAINT trades_pkey;
                ALTER TABLE trades ADD PRIMARY KEY (id, ts);
            END IF;

            IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'ux_trades_tx_hash')
               AND NOT EXISTS (
                   SELECT 1 FROM pg_indexes
                   WHERE indexname = 'ux_trades_tx_hash_ts'
               ) THEN
                DROP INDEX ux_trades_tx_hash;
                CREATE UNIQUE INDEX ux_trades_tx_hash_ts
                    ON trades (tx_hash, ts)
                    WHERE tx_hash IS NOT NULL;
            END IF;
        END $$;
    """)

    # Step 2 — convert to hypertable (online, brief lock per chunk)
    op.execute("""
        SELECT create_hypertable(
            'trades', 'ts',
            chunk_time_interval => INTERVAL '7 days',
            migrate_data => true,
            if_not_exists => TRUE
        )
    """)

    # Step 3 — 180-day retention. Background job drops old chunks daily.
    op.execute("""
        SELECT add_retention_policy(
            'trades',
            INTERVAL '180 days',
            if_not_exists => TRUE
        )
    """)


def downgrade() -> None:
    op.execute("SELECT remove_retention_policy('trades', if_exists => TRUE)")
    # Not reversing the hypertable conversion or the composite PK — those
    # would require a full data copy and we'd lose chunk structure. Manual
    # ops if you really need to roll back this far.
