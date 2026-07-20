"""Add decision_features table for ML-overlay Phase 1 feature-snapshot logging.

Revision ID: 008
Revises: 007
Create Date: 2026-07-13
"""
from __future__ import annotations
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Decision matrix at design time:
    #   - mode column distinguishes manual-queue proposals from auto executions
    #     (each row would otherwise look identical to a Phase 2 trainer; manual
    #     rows are failures from training's POV). Default 'auto' matches the
    #     dominant path; manual rows are populated by pipeline._log_decision_features.
    #   - llm_action/strength/rationale columns are DELIBERATELY omitted — see
    #     the plan's "Architecture" preamble above for the drop-vs-back-fill
    #     decision and the rationale.
    op.execute("""
        CREATE TABLE IF NOT EXISTS decision_features (
            id SERIAL PRIMARY KEY,
            run_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            strategy TEXT NOT NULL,
            regime TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'auto'
                CHECK (mode IN ('auto', 'manual')),
            signal_strength_pre_overlay REAL NOT NULL,
            features JSONB NOT NULL,
            order_id INTEGER,
            backfilled BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_features_symbol_ts "
        "ON decision_features(symbol, ts DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_features_order_id "
        "ON decision_features(order_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS decision_features")
