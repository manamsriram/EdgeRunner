"""Add trade_outcomes table and orders.entry_rationale column

Revision ID: 003
Revises: 002
Create Date: 2026-07-02
"""
from __future__ import annotations
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS trade_outcomes (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            strategy TEXT NOT NULL,
            regime TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            pnl_pct REAL NOT NULL,
            exit_reason TEXT NOT NULL,
            entry_overlay_rationale TEXT,
            closed_at TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_trade_outcomes_symbol ON trade_outcomes(symbol, closed_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trade_outcomes_arm ON trade_outcomes(strategy, regime, closed_at DESC)")
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS entry_rationale TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS entry_rationale")
    op.execute("DROP TABLE IF EXISTS trade_outcomes")
