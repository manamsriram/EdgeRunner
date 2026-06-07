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
    broker_order_id TEXT
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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresRepository(PortfolioRepository):
    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._init_schema()

    def _connect(self) -> psycopg2.extensions.connection:
        conn = psycopg2.connect(self._url, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA)

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
                    "(client_order_id, ts, symbol, side, notional, status, broker_order_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (client_order_id) DO UPDATE SET "
                    "status=EXCLUDED.status, "
                    "broker_order_id=COALESCE(EXCLUDED.broker_order_id, orders.broker_order_id) "
                    "RETURNING id",
                    (order.client_order_id, _now(), order.symbol, order.side,
                     order.notional, order.status, order.broker_order_id),
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
