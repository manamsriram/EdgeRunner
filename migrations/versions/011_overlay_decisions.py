"""overlay_prompts + overlay_decisions: capture the text the LLM actually sees

Phase 1 of the ML overlay logged a 22-number feature vector per decision. Training on
it plateaued at ~73% agreement with the LLM across rolling-origin folds — against a
~96% ceiling set by the LLM's own self-consistency, and with gradient boosting scoring
identically to logistic regression, which is the signature of missing information
rather than missing model capacity.

The prompt (claude_overlay.py) carries three things the feature vector does not:
full news headline text, the strategy's free-text signal reason, and the LLM's own
rationale. These tables persist them so a later retrain can use them.

Prompts are stored once per distinct hash: the 30-minute overlay cache replays the
same prompt many times, and the raw row count is ~4.4x the count of distinct decisions.

Revision ID: 011
Revises: 010
"""
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS overlay_prompts (
            prompt_hash TEXT PRIMARY KEY,
            prompt_text TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS overlay_decisions (
            id            SERIAL PRIMARY KEY,
            run_id        INTEGER NOT NULL,
            ts            TEXT NOT NULL,
            symbol        TEXT NOT NULL,
            prompt_hash   TEXT NOT NULL REFERENCES overlay_prompts(prompt_hash),
            action        TEXT NOT NULL,
            strength_post REAL NOT NULL,
            rationale     TEXT NOT NULL,
            provider      TEXT NOT NULL
        )
    """)
    # Training joins decision_features to this on (run_id, symbol).
    op.execute("CREATE INDEX IF NOT EXISTS idx_overlay_decisions_run_symbol "
               "ON overlay_decisions(run_id, symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_overlay_decisions_ts "
               "ON overlay_decisions(ts)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS overlay_decisions")
    op.execute("DROP TABLE IF EXISTS overlay_prompts")
