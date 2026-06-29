"""Add pool column to position_owners; change PK to (symbol, pool)

Revision ID: 002
Revises: 001
Create Date: 2026-06-28
"""
from __future__ import annotations
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Split into separate execute calls — multi-statement op.execute can silently skip later statements
    # in some SQLAlchemy/psycopg2 versions.
    op.execute("ALTER TABLE position_owners ADD COLUMN IF NOT EXISTS pool VARCHAR(10) NOT NULL DEFAULT 'daily'")
    op.execute("ALTER TABLE position_owners DROP CONSTRAINT IF EXISTS position_owners_pkey")
    op.execute("ALTER TABLE position_owners ADD PRIMARY KEY (symbol, pool)")


def downgrade() -> None:
    op.execute("ALTER TABLE position_owners DROP CONSTRAINT IF EXISTS position_owners_pkey")
    op.execute("ALTER TABLE position_owners ADD PRIMARY KEY (symbol)")
    op.execute("ALTER TABLE position_owners DROP COLUMN IF EXISTS pool")
