"""Add llm_call_log table (Phase 0 of the ML-overlay research plan)

Revision ID: 004
Revises: 003
Create Date: 2026-07-06
"""
from __future__ import annotations
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS llm_call_log (
            id SERIAL PRIMARY KEY,
            ts TEXT NOT NULL,
            provider TEXT NOT NULL,
            call_site TEXT NOT NULL,
            symbol TEXT NOT NULL,
            cache_hit BOOLEAN NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            est_cost_usd REAL NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_llm_call_log_ts ON llm_call_log(ts)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm_call_log")
