"""Add options_positions table (CSP-on-dip + Wheel strategy)

Revision ID: 005
Revises: 004
Create Date: 2026-07-07
"""
from __future__ import annotations
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS options_positions (
            id               SERIAL PRIMARY KEY,
            contract_symbol  TEXT NOT NULL UNIQUE,
            underlying       TEXT NOT NULL,
            option_type      TEXT NOT NULL,
            strike           REAL NOT NULL,
            expiry           TEXT NOT NULL,
            opening_order_id TEXT NOT NULL,
            strategy         TEXT NOT NULL,
            collateral       REAL NOT NULL,
            wheel_state      TEXT NOT NULL DEFAULT 'csp_open',
            status           TEXT NOT NULL DEFAULT 'open',
            opened_at        TEXT NOT NULL
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_options_positions_underlying "
        "ON options_positions(underlying, status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS options_positions")
