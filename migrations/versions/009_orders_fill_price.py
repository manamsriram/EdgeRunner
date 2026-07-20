"""Add orders.fill_price + index for highest-open-buy-price lookups

Revision ID: 009
Revises: 008
Create Date: 2026-07-20

Enables anchoring a symbol's protective stop to the highest price paid across
its currently open lots, instead of the newest fill or the broker's averaged
cost basis — averaging down was masking how underwater the worst lot was.
"""
from __future__ import annotations
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS fill_price REAL")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_symbol_side_status_ts "
        "ON orders(symbol, side, status, ts)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_orders_symbol_side_status_ts")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS fill_price")
