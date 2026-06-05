"""SQLite implementation of PortfolioRepository.

Shares the same DB file as the existing app.py auth store (`users`/`queries`) — the new
trading tables are created alongside with CREATE TABLE IF NOT EXISTS, so there is NO
destructive migration of real user data. A Postgres/Supabase adapter replaces this class
behind the same interface later.

Concurrency: the scheduler and the Streamlit dashboard touch this DB at once, so we open
in WAL mode with a busy timeout and use a short-lived connection per call. `check_same_thread=False` lets a connection cross threads safely given the per-call pattern.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

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
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    strategy  TEXT NOT NULL,
    mode      TEXT NOT NULL,
    note      TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    side    TEXT NOT NULL,
    strength REAL NOT NULL,
    reason  TEXT,
    FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    ts              TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    notional        REAL NOT NULL,
    status          TEXT NOT NULL,
    broker_order_id TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side   TEXT NOT NULL,
    qty    REAL NOT NULL,
    price  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
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


class SQLiteRepository(PortfolioRepository):
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # WAL lets readers (dashboard) and a writer (scheduler) proceed concurrently.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ---- writes ----

    def record_run(self, strategy: str, mode: str, note: str = "") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, strategy, mode, note) VALUES (?, ?, ?, ?)",
                (_now(), strategy, mode, note),
            )
            return int(cur.lastrowid)

    def record_signal(self, signal: SignalRow) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO signals (run_id, ts, symbol, side, strength, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (signal.run_id, _now(), signal.symbol, signal.side,
                 signal.strength, signal.reason),
            )
            return int(cur.lastrowid)

    def record_order(self, order: OrderRow) -> int:
        with self._connect() as conn:
            # Idempotent at the DB layer too: a repeated client_order_id is ignored,
            # then we return the existing row's id.
            conn.execute(
                "INSERT INTO orders "
                "(client_order_id, ts, symbol, side, notional, status, broker_order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(client_order_id) DO UPDATE SET "
                "status=excluded.status, "
                "broker_order_id=COALESCE(excluded.broker_order_id, orders.broker_order_id)",
                (order.client_order_id, _now(), order.symbol, order.side,
                 order.notional, order.status, order.broker_order_id),
            )
            row = conn.execute(
                "SELECT id FROM orders WHERE client_order_id=?",
                (order.client_order_id,),
            ).fetchone()
            return int(row["id"])

    def record_trade(self, trade: TradeRow) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO trades (ts, symbol, side, qty, price) VALUES (?, ?, ?, ?, ?)",
                (_now(), trade.symbol, trade.side, trade.qty, trade.price),
            )
            return int(cur.lastrowid)

    def create_proposal(self, proposal: ProposalRow) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO proposals "
                "(created_at, symbol, side, notional, ref_price, reason, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), proposal.symbol, proposal.side, proposal.notional,
                 proposal.ref_price, proposal.reason, PROPOSAL_PENDING),
            )
            return int(cur.lastrowid)

    def set_proposal_status(self, proposal_id: int, status: str) -> None:
        from trader.portfolio.repository import (
            PROPOSAL_APPROVED, PROPOSAL_EXECUTED, PROPOSAL_REJECTED,
        )
        valid = {PROPOSAL_PENDING, PROPOSAL_APPROVED, PROPOSAL_REJECTED, PROPOSAL_EXECUTED}
        if status not in valid:
            raise ValueError(f"invalid proposal status {status!r}; must be one of {valid}")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE proposals SET status=?, decided_at=? WHERE id=?",
                (status, _now(), proposal_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"proposal {proposal_id} not found")

    # ---- reads ----

    def list_pending_proposals(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE status=? ORDER BY id",
                (PROPOSAL_PENDING,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_orders(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_runs(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, started_at, strategy, mode, note FROM runs ORDER BY id DESC LIMIT 20"
            ).fetchall()
            return [dict(r) for r in rows]
