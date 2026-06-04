"""initial schema + timescale hypertable on trades

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-01

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # wallets ----------------------------------------------------------------
    op.create_table(
        "wallets",
        sa.Column("address", sa.String(42), primary_key=True),
        sa.Column("label", sa.String(128)),
        sa.Column("category", sa.String(64), index=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
    )
    op.create_index("ix_wallets_is_active", "wallets", ["is_active"])

    op.create_table(
        "wallet_stats",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("address", sa.String(42), index=True),
        sa.Column("window", sa.String(16)),
        sa.Column("pnl_usdc", sa.Float, server_default="0"),
        sa.Column("roi", sa.Float, server_default="0"),
        sa.Column("win_rate", sa.Float, server_default="0"),
        sa.Column("sharpe", sa.Float, server_default="0"),
        sa.Column("trade_count", sa.Integer, server_default="0"),
        sa.Column("avg_trade_size", sa.Float, server_default="0"),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_wallet_stats_addr_window", "wallet_stats", ["address", "window"])

    # markets ----------------------------------------------------------------
    op.create_table(
        "markets",
        sa.Column("market_id", sa.String(80), primary_key=True),
        sa.Column("slug", sa.String(256), index=True),
        sa.Column("question", sa.Text),
        sa.Column("category", sa.String(64), index=True),
        sa.Column("end_date", sa.DateTime(timezone=True), index=True),
        sa.Column("resolved", sa.Boolean, server_default=sa.text("false")),
        sa.Column("outcome", sa.String(32)),
        sa.Column("liquidity_usdc", sa.Float, server_default="0"),
        sa.Column("volume_24h_usdc", sa.Float, server_default="0"),
        sa.Column("yes_token_id", sa.String(80)),
        sa.Column("no_token_id", sa.String(80)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    # trades — hypertable on ts ---------------------------------------------
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tx_hash", sa.String(80), index=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("wallet", sa.String(42), index=True),
        sa.Column("market_id", sa.String(80), index=True),
        sa.Column("outcome", sa.String(8)),
        sa.Column("side", sa.String(8)),
        sa.Column("size_shares", sa.Float),
        sa.Column("price", sa.Float),
        sa.Column("notional_usdc", sa.Float),
        sa.Column("fee_usdc", sa.Float, server_default="0"),
        sa.Column("source", sa.String(16)),
    )
    op.create_index("ix_trades_wallet_ts", "trades", ["wallet", "ts"])
    op.create_index("ix_trades_market_ts", "trades", ["market_id", "ts"])
    # Try to convert to a TimescaleDB hypertable. If it fails (e.g. because the
    # primary key doesn't include `ts`, or the extension isn't installed), we
    # log a NOTICE and continue — the table works fine as a normal pg table.
    op.execute("""
        DO $$
        BEGIN
            PERFORM 1 FROM pg_extension WHERE extname='timescaledb';
            IF FOUND THEN
                BEGIN
                    PERFORM create_hypertable('trades', 'ts',
                        chunk_time_interval => INTERVAL '7 days',
                        if_not_exists => TRUE,
                        migrate_data => TRUE);
                EXCEPTION WHEN OTHERS THEN
                    RAISE NOTICE 'hypertable conversion skipped: %', SQLERRM;
                END;
            END IF;
        END$$;
    """)

    # positions --------------------------------------------------------------
    op.create_table(
        "positions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.String(42), index=True),
        sa.Column("market_id", sa.String(80), index=True),
        sa.Column("outcome", sa.String(8)),
        sa.Column("size_shares", sa.Float, server_default="0"),
        sa.Column("avg_price", sa.Float, server_default="0"),
        sa.Column("realized_pnl_usdc", sa.Float, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ux_positions_wallet_market", "positions",
                    ["wallet", "market_id", "outcome"], unique=True)

    # signals ----------------------------------------------------------------
    op.create_table(
        "signals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), index=True),
        sa.Column("market_id", sa.String(80), index=True),
        sa.Column("outcome", sa.String(8)),
        sa.Column("side", sa.String(8)),
        sa.Column("wallet_count", sa.Integer),
        sa.Column("wallets", sa.JSON),
        sa.Column("avg_win_rate", sa.Float),
        sa.Column("correlation_score", sa.Float),
        sa.Column("target_price", sa.Float),
        sa.Column("target_size_usdc", sa.Float),
        sa.Column("gate_results", sa.JSON),
        sa.Column("gate_pass", sa.Boolean, index=True),
        sa.Column("executed", sa.Boolean, server_default=sa.text("false"), index=True),
        sa.Column("notes", sa.Text),
    )

    # fills ------------------------------------------------------------------
    op.create_table(
        "fills",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.Integer, sa.ForeignKey("signals.id"), index=True),
        sa.Column("ts", sa.DateTime(timezone=True), index=True),
        sa.Column("mode", sa.String(8)),
        sa.Column("market_id", sa.String(80), index=True),
        sa.Column("outcome", sa.String(8)),
        sa.Column("side", sa.String(8)),
        sa.Column("size_shares", sa.Float),
        sa.Column("price", sa.Float),
        sa.Column("notional_usdc", sa.Float),
        sa.Column("fee_usdc", sa.Float, server_default="0"),
        sa.Column("status", sa.String(16)),
        sa.Column("venue_order_id", sa.String(128)),
        sa.Column("error", sa.String(512)),
    )

    # pnl --------------------------------------------------------------------
    op.create_table(
        "pnl_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), index=True),
        sa.Column("mode", sa.String(8), index=True),
        sa.Column("equity_usdc", sa.Float),
        sa.Column("realized_usdc", sa.Float, server_default="0"),
        sa.Column("unrealized_usdc", sa.Float, server_default="0"),
        sa.Column("open_positions", sa.Integer, server_default="0"),
    )

    # audit ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), index=True),
        sa.Column("actor", sa.String(64), index=True),
        sa.Column("event", sa.String(64), index=True),
        sa.Column("payload", sa.JSON),
    )


def downgrade() -> None:
    for t in ["audit_log", "pnl_snapshots", "fills", "signals",
              "positions", "trades", "markets", "wallet_stats", "wallets"]:
        op.drop_table(t)
