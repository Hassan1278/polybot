"""realised stats columns + outcome/side widening + tx_hash unique index

Revision ID: 0002_realised_stats_and_widening
Revises: 0001_initial
Create Date: 2026-06-04

Folds in the ad-hoc patches that were previously applied via
scripts/_schema_patch.sql and PowerShell ALTER TABLE one-liners:

- wallet_stats: make win_rate/sharpe nullable; add realized_pnl_usdc,
  n_decisions, n_open_positions, n_total_positions, n_trade_days
  (all FLOAT/INTEGER default 0).
- Widen outcome/side string columns on trades, signals, fills, positions
  so longer outcome labels (e.g. multi-outcome markets) and side values
  like "BUY"/"SELL" fit.
- Add a partial UNIQUE index on trades(tx_hash) WHERE tx_hash IS NOT NULL
  so on-chain ingest can dedupe without rejecting NULL hashes from
  manual/backfill rows.

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_realised_stats_and_widening"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # wallet_stats: relax NOT NULL on rate metrics ---------------------------
    op.alter_column("wallet_stats", "win_rate",
                    existing_type=sa.Float(), nullable=True,
                    server_default=None)
    op.alter_column("wallet_stats", "sharpe",
                    existing_type=sa.Float(), nullable=True,
                    server_default=None)

    # wallet_stats: new aggregate columns ------------------------------------
    op.add_column("wallet_stats",
                  sa.Column("realized_pnl_usdc", sa.Float, server_default="0"))
    op.add_column("wallet_stats",
                  sa.Column("n_decisions", sa.Integer, server_default="0"))
    op.add_column("wallet_stats",
                  sa.Column("n_open_positions", sa.Integer, server_default="0"))
    op.add_column("wallet_stats",
                  sa.Column("n_total_positions", sa.Integer, server_default="0"))
    op.add_column("wallet_stats",
                  sa.Column("n_trade_days", sa.Integer, server_default="0"))

    # widen outcome/side on trades -------------------------------------------
    op.alter_column("trades", "outcome",
                    existing_type=sa.String(8), type_=sa.String(64))
    op.alter_column("trades", "side",
                    existing_type=sa.String(8), type_=sa.String(16))

    # widen outcome/side on signals ------------------------------------------
    op.alter_column("signals", "outcome",
                    existing_type=sa.String(8), type_=sa.String(64))
    op.alter_column("signals", "side",
                    existing_type=sa.String(8), type_=sa.String(16))

    # widen outcome/side on fills --------------------------------------------
    op.alter_column("fills", "outcome",
                    existing_type=sa.String(8), type_=sa.String(64))
    op.alter_column("fills", "side",
                    existing_type=sa.String(8), type_=sa.String(16))

    # widen outcome on positions ---------------------------------------------
    op.alter_column("positions", "outcome",
                    existing_type=sa.String(8), type_=sa.String(64))

    # partial unique index on trades.tx_hash ---------------------------------
    # op.create_index() has no IF NOT EXISTS, so emit raw SQL for idempotency.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_tx_hash "
        "ON trades (tx_hash) WHERE tx_hash IS NOT NULL"
    )


def downgrade() -> None:
    # drop the partial unique index ------------------------------------------
    op.execute("DROP INDEX IF EXISTS ux_trades_tx_hash")

    # The original downgrade narrowed String(64) → String(8) without first
    # truncating data — a real prod DB has outcomes like 'TYLOO' or
    # 'LYNN VISION' that would either error or silently truncate on the
    # ALTER. We pre-truncate with LEFT(col, 8) so the narrow is at least
    # safe-by-contract; do the same for the side columns whose String(16)
    # values include 'BUY'/'SELL' (both ≤ 4 chars so they round-trip).
    #
    # If you're rolling back on an empty DB this is a no-op. On a populated
    # DB you accept the lossy column truncation as part of the downgrade.
    op.execute("UPDATE positions SET outcome = LEFT(outcome, 8) WHERE LENGTH(outcome) > 8")
    op.alter_column("positions", "outcome",
                    existing_type=sa.String(64), type_=sa.String(8))

    op.execute("UPDATE fills SET side = LEFT(side, 8) WHERE LENGTH(side) > 8")
    op.alter_column("fills", "side",
                    existing_type=sa.String(16), type_=sa.String(8))
    op.execute("UPDATE fills SET outcome = LEFT(outcome, 8) WHERE LENGTH(outcome) > 8")
    op.alter_column("fills", "outcome",
                    existing_type=sa.String(64), type_=sa.String(8))

    op.execute("UPDATE signals SET side = LEFT(side, 8) WHERE LENGTH(side) > 8")
    op.alter_column("signals", "side",
                    existing_type=sa.String(16), type_=sa.String(8))
    op.execute("UPDATE signals SET outcome = LEFT(outcome, 8) WHERE LENGTH(outcome) > 8")
    op.alter_column("signals", "outcome",
                    existing_type=sa.String(64), type_=sa.String(8))

    op.execute("UPDATE trades SET side = LEFT(side, 8) WHERE LENGTH(side) > 8")
    op.alter_column("trades", "side",
                    existing_type=sa.String(16), type_=sa.String(8))
    op.execute("UPDATE trades SET outcome = LEFT(outcome, 8) WHERE LENGTH(outcome) > 8")
    op.alter_column("trades", "outcome",
                    existing_type=sa.String(64), type_=sa.String(8))

    # drop wallet_stats aggregate columns ------------------------------------
    op.drop_column("wallet_stats", "n_trade_days")
    op.drop_column("wallet_stats", "n_total_positions")
    op.drop_column("wallet_stats", "n_open_positions")
    op.drop_column("wallet_stats", "n_decisions")
    op.drop_column("wallet_stats", "realized_pnl_usdc")

    # Backfill NULLs before restoring NOT NULL — the original ALTER would
    # fail on any wallet without enough realised data (we explicitly made
    # these nullable in upgrade() for that very reason).
    op.execute("UPDATE wallet_stats SET win_rate = 0 WHERE win_rate IS NULL")
    op.execute("UPDATE wallet_stats SET sharpe   = 0 WHERE sharpe   IS NULL")
    op.alter_column("wallet_stats", "sharpe",
                    existing_type=sa.Float(), nullable=False,
                    server_default="0")
    op.alter_column("wallet_stats", "win_rate",
                    existing_type=sa.Float(), nullable=False,
                    server_default="0")
