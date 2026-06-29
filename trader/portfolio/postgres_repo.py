"""Postgres implementation of PortfolioRepository.

Activated when DATABASE_URL is set in the environment. Mirrors SQLiteRepository
exactly — same method signatures, same short-lived connection pattern, same
idempotent schema init. Uses psycopg2 (sync) to match the blocking call sites.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

from trader.portfolio.repository import (
    PROPOSAL_PENDING,
    OrderRow,
    PortfolioRepository,
    ProposalRow,
    SignalRow,
    TradeRow,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          SERIAL PRIMARY KEY,
    started_at  TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    mode        TEXT NOT NULL,
    note        TEXT
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
CREATE INDEX IF NOT EXISTS idx_arm_ic ON arm_ic_series(strategy_name, regime, ts DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresRepository(PortfolioRepository):
    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._init_schema()

    def _connect(self) -> psycopg2.extensions.connection:
        conn = psycopg2.connect(self._url, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10)
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA)
                cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS strategy_name TEXT")
                cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS regime TEXT")
                cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS signal_strength REAL")
                cur.execute("ALTER TABLE bandit_weights ADD COLUMN IF NOT EXISTS alpha_wins INTEGER NOT NULL DEFAULT 1")
                cur.execute("ALTER TABLE bandit_weights ADD COLUMN IF NOT EXISTS beta_losses INTEGER NOT NULL DEFAULT 1")
                cur.execute("ALTER TABLE position_owners ADD COLUMN IF NOT EXISTS pool VARCHAR(10) NOT NULL DEFAULT 'daily'")

    # ---- writes ----

    def record_run(self, strategy: str, mode: str, note: str = "") -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO runs (started_at, strategy, mode, note) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (_now(), strategy, mode, note),
                )
                return int(cur.fetchone()["id"])

    def record_signal(self, signal: SignalRow) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO signals (run_id, ts, symbol, side, strength, reason) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (signal.run_id, _now(), signal.symbol, signal.side,
                     signal.strength, signal.reason),
                )
                return int(cur.fetchone()["id"])

    def record_order(self, order: OrderRow) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO orders "
                    "(client_order_id, ts, symbol, side, notional, status, broker_order_id, "
                    "strategy_name, regime, signal_strength) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (client_order_id) DO UPDATE SET "
                    "status=EXCLUDED.status, "
                    "broker_order_id=COALESCE(EXCLUDED.broker_order_id, orders.broker_order_id) "
                    "RETURNING id",
                    (order.client_order_id, _now(), order.symbol, order.side,
                     order.notional, order.status, order.broker_order_id,
                     order.strategy_name, order.regime, order.signal_strength),
                )
                return int(cur.fetchone()["id"])

    def record_trade(self, trade: TradeRow) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO trades (ts, symbol, side, qty, price) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (_now(), trade.symbol, trade.side, trade.qty, trade.price),
                )
                return int(cur.fetchone()["id"])

    def create_proposal(self, proposal: ProposalRow) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO proposals "
                    "(created_at, symbol, side, notional, ref_price, reason, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (_now(), proposal.symbol, proposal.side, proposal.notional,
                     proposal.ref_price, proposal.reason, PROPOSAL_PENDING),
                )
                return int(cur.fetchone()["id"])

    def set_proposal_status(self, proposal_id: int, status: str) -> None:
        from trader.portfolio.repository import (
            PROPOSAL_APPROVED, PROPOSAL_EXECUTED, PROPOSAL_REJECTED,
        )
        valid = {PROPOSAL_PENDING, PROPOSAL_APPROVED, PROPOSAL_REJECTED, PROPOSAL_EXECUTED}
        if status not in valid:
            raise ValueError(f"invalid proposal status {status!r}; must be one of {valid}")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE proposals SET status=%s, decided_at=%s WHERE id=%s",
                    (status, _now(), proposal_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"proposal {proposal_id} not found")

    def try_approve_proposal(self, proposal_id: int) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE proposals SET status=%s, decided_at=%s WHERE id=%s AND status=%s",
                    (PROPOSAL_APPROVED, _now(), proposal_id, PROPOSAL_PENDING),
                )
                if cur.rowcount == 0:
                    return None
                cur.execute("SELECT * FROM proposals WHERE id=%s", (proposal_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    # ---- reads ----

    def list_pending_proposals(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM proposals WHERE status=%s ORDER BY id",
                    (PROPOSAL_PENDING,),
                )
                return [dict(r) for r in cur.fetchall()]

    def get_orders(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM orders ORDER BY id")
                return [dict(r) for r in cur.fetchall()]

    def get_runs(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, started_at, strategy, mode, note FROM runs ORDER BY id DESC LIMIT 20"
                )
                return [dict(r) for r in cur.fetchall()]

    def get_strategy_signal_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT r.strategy, COUNT(*) AS cnt "
                    "FROM signals s JOIN runs r ON s.run_id = r.id "
                    "WHERE r.mode = 'auto' GROUP BY r.strategy"
                )
                return {row["strategy"]: row["cnt"] for row in cur.fetchall()}

    def get_position_owners(self) -> dict[tuple[str, str], str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT symbol, pool, strategy FROM position_owners")
                return {(row["symbol"], row["pool"]): row["strategy"] for row in cur.fetchall()}

    def set_position_owner(self, symbol: str, strategy: str, pool: str = "daily") -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO position_owners (symbol, pool, strategy, updated_at) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (symbol, pool) DO UPDATE SET strategy=EXCLUDED.strategy, updated_at=EXCLUDED.updated_at",
                    (symbol, pool, strategy, _now()),
                )

    def clear_position_owner(self, symbol: str, pool: str = "daily") -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM position_owners WHERE symbol=%s AND pool=%s",
                    (symbol, pool),
                )

    def get_bandit_weight(self, strategy: str, regime: str) -> float:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT weight FROM bandit_weights WHERE strategy_name=%s AND regime=%s",
                    (strategy, regime),
                )
                row = cur.fetchone()
                return float(row["weight"]) if row else 1.0

    def save_bandit_weight(self, strategy: str, regime: str, weight: float, cycle_index: int) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bandit_weights (strategy_name, regime, weight, cycle_index, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (strategy_name, regime) DO UPDATE SET "
                    "weight=EXCLUDED.weight, cycle_index=EXCLUDED.cycle_index, updated_at=EXCLUDED.updated_at",
                    (strategy, regime, weight, cycle_index, _now()),
                )

    def get_all_bandit_weights(self) -> dict[tuple[str, str], tuple[float, int]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT strategy_name, regime, weight, cycle_index FROM bandit_weights"
                )
                return {
                    (r["strategy_name"], r["regime"]): (float(r["weight"]), int(r["cycle_index"]))
                    for r in cur.fetchall()
                }

    def save_bandit_arm(self, strategy: str, regime: str,
                        alpha: int, beta: int, cycle_index: int, weight: float) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bandit_weights "
                    "(strategy_name, regime, weight, cycle_index, updated_at, alpha_wins, beta_losses) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (strategy_name, regime) DO UPDATE SET "
                    "weight=EXCLUDED.weight, cycle_index=EXCLUDED.cycle_index, "
                    "updated_at=EXCLUDED.updated_at, alpha_wins=EXCLUDED.alpha_wins, "
                    "beta_losses=EXCLUDED.beta_losses",
                    (strategy, regime, weight, cycle_index, _now(), alpha, beta),
                )

    def get_all_bandit_arms(self) -> dict[tuple[str, str], tuple[int, int, int]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT strategy_name, regime, alpha_wins, beta_losses, cycle_index "
                    "FROM bandit_weights"
                )
                return {
                    (r["strategy_name"], r["regime"]): (
                        int(r["alpha_wins"]), int(r["beta_losses"]), int(r["cycle_index"])
                    )
                    for r in cur.fetchall()
                }

    def append_ic_observation(self, strategy: str, regime: str, ic: float, ts: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO arm_ic_series (strategy_name, regime, ic, ts) "
                    "VALUES (%s, %s, %s, %s)",
                    (strategy, regime, ic, ts),
                )

    def get_ic_series(self, strategy: str, regime: str, limit: int = 60) -> list[float]:
        # ORDER BY ts works correctly only when ts values are full ISO-8601 UTC strings
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ic FROM arm_ic_series WHERE strategy_name=%s AND regime=%s "
                    "ORDER BY ts DESC LIMIT %s",
                    (strategy, regime, limit),
                )
                rows = cur.fetchall()
        return [float(r["ic"]) for r in reversed(rows)]
