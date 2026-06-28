"""baseline schema

Revision ID: 001
Revises:
Create Date: 2026-06-27

Captures the full schema as of the initial Alembic integration.
All statements are idempotent (IF NOT EXISTS) so this is safe
to run against both fresh installs and the existing live database.
"""
from __future__ import annotations

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id         SERIAL PRIMARY KEY,
            started_at TEXT NOT NULL,
            strategy   TEXT NOT NULL,
            mode       TEXT NOT NULL,
            note       TEXT
        );

        CREATE TABLE IF NOT EXISTS signals (
            id       SERIAL PRIMARY KEY,
            run_id   INT NOT NULL,
            ts       TEXT NOT NULL,
            symbol   TEXT NOT NULL,
            side     TEXT NOT NULL,
            strength REAL NOT NULL,
            reason   TEXT,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id              SERIAL PRIMARY KEY,
            client_order_id TEXT NOT NULL UNIQUE,
            ts              TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            side            TEXT NOT NULL,
            notional        REAL NOT NULL,
            status          TEXT NOT NULL,
            broker_order_id TEXT,
            strategy_name   TEXT,
            regime          TEXT,
            signal_strength REAL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id     SERIAL PRIMARY KEY,
            ts     TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side   TEXT NOT NULL,
            qty    REAL NOT NULL,
            price  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS proposals (
            id         SERIAL PRIMARY KEY,
            created_at TEXT NOT NULL,
            symbol     TEXT NOT NULL,
            side       TEXT NOT NULL,
            notional   REAL NOT NULL,
            ref_price  REAL NOT NULL,
            reason     TEXT,
            status     TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS position_owners (
            symbol     TEXT PRIMARY KEY,
            strategy   TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bandit_weights (
            strategy_name TEXT NOT NULL,
            regime        TEXT NOT NULL,
            weight        REAL NOT NULL,
            cycle_index   INT NOT NULL,
            updated_at    TEXT NOT NULL,
            alpha_wins    INT NOT NULL DEFAULT 1,
            beta_losses   INT NOT NULL DEFAULT 1,
            PRIMARY KEY (strategy_name, regime)
        );

        CREATE TABLE IF NOT EXISTS arm_ic_series (
            id            SERIAL PRIMARY KEY,
            strategy_name TEXT NOT NULL,
            regime        TEXT NOT NULL,
            ic            REAL NOT NULL,
            ts            TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_arm_ic
            ON arm_ic_series(strategy_name, regime, ts DESC);

        -- Columns added after initial deploy — safe no-ops on fresh installs
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS signal_strength REAL;
        ALTER TABLE bandit_weights
            ADD COLUMN IF NOT EXISTS alpha_wins INT NOT NULL DEFAULT 1;
        ALTER TABLE bandit_weights
            ADD COLUMN IF NOT EXISTS beta_losses INT NOT NULL DEFAULT 1;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS arm_ic_series;
        DROP TABLE IF EXISTS bandit_weights;
        DROP TABLE IF EXISTS position_owners;
        DROP TABLE IF EXISTS proposals;
        DROP TABLE IF EXISTS trades;
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS signals;
        DROP TABLE IF EXISTS runs;
    """)
