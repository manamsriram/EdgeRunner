"""Add CHECK constraints on options_positions.wheel_state/status

Revision ID: 006
Revises: 005
Create Date: 2026-07-07
"""
from __future__ import annotations
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE options_positions ADD CONSTRAINT options_positions_wheel_state_check "
        "CHECK (wheel_state IN ('csp_open', 'assigned', 'cc_open', 'called_away', 'csp_expired', 'cc_expired'))"
    )
    op.execute(
        "ALTER TABLE options_positions ADD CONSTRAINT options_positions_status_check "
        "CHECK (status IN ('open', 'closed'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE options_positions DROP CONSTRAINT IF EXISTS options_positions_wheel_state_check")
    op.execute("ALTER TABLE options_positions DROP CONSTRAINT IF EXISTS options_positions_status_check")
