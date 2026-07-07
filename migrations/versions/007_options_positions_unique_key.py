"""Re-key options_positions uniqueness from contract_symbol to opening_order_id

contract_symbol (the OCC symbol) can recur across separate Wheel cycles — the same
strike/expiry can legitimately be sold again after a prior position on it closed.
UNIQUE(contract_symbol) silently blocked recording that legitimate re-open (INSERT
... ON CONFLICT DO NOTHING would no-op, and the caller got back the *old closed* row's
id). Idempotency belongs on opening_order_id (one row per broker order), not the
contract symbol.

Revision ID: 007
Revises: 006
Create Date: 2026-07-07
"""
from __future__ import annotations
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$
        DECLARE
            conname text;
        BEGIN
            SELECT c.conname INTO conname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = 'options_positions' AND c.contype = 'u'
              AND c.conkey = (
                  SELECT array_agg(attnum) FROM pg_attribute
                  WHERE attrelid = t.oid AND attname = 'contract_symbol'
              );
            IF conname IS NOT NULL THEN
                EXECUTE format('ALTER TABLE options_positions DROP CONSTRAINT %I', conname);
            END IF;
        END $$;
    """)
    op.execute(
        "ALTER TABLE options_positions ADD CONSTRAINT options_positions_opening_order_id_key "
        "UNIQUE (opening_order_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_options_positions_contract_symbol "
        "ON options_positions(contract_symbol)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE options_positions DROP CONSTRAINT IF EXISTS options_positions_opening_order_id_key")
    op.execute("DROP INDEX IF EXISTS idx_options_positions_contract_symbol")
    op.execute("ALTER TABLE options_positions ADD CONSTRAINT options_positions_contract_symbol_key UNIQUE (contract_symbol)")
