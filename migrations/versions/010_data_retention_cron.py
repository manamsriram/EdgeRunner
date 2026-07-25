"""Add pg_cron job to purge old diagnostic/log rows (30-day retention).

Revision ID: 010
Revises: 009
Create Date: 2026-07-23

Prunes signals, arm_ic_series, llm_call_log, decision_features — high-volume
append-only diagnostic tables with no retention need. Leaves trades/orders/
proposals untouched (audit trail, low volume). Supabase free tier is
storage-capped; this keeps the DB small automatically via native pg_cron
instead of app-side cleanup code.
"""
from __future__ import annotations

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_cron")
    op.execute("""
        SELECT cron.schedule(
            'purge_old_diagnostics',
            '0 3 * * *',
            $$
            DELETE FROM signals WHERE ts::timestamptz < now() - interval '30 days';
            DELETE FROM arm_ic_series WHERE ts::timestamptz < now() - interval '30 days';
            DELETE FROM llm_call_log WHERE ts::timestamptz < now() - interval '30 days';
            DELETE FROM decision_features WHERE ts::timestamptz < now() - interval '30 days';
            $$
        );
    """)


def downgrade() -> None:
    op.execute("SELECT cron.unschedule('purge_old_diagnostics')")
