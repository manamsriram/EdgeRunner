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
    TradeOutcomeRow,
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
    broker_order_id TEXT,
    strategy_name   TEXT,
    regime          TEXT
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
CREATE TABLE IF NOT EXISTS position_owners (
    symbol     TEXT NOT NULL,
    pool       TEXT NOT NULL DEFAULT 'daily',
    strategy   TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, pool)
);
CREATE TABLE IF NOT EXISTS bandit_weights (
    strategy_name TEXT NOT NULL,
    regime        TEXT NOT NULL,
    weight        REAL NOT NULL,
    cycle_index   INTEGER NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (strategy_name, regime)
);
CREATE TABLE IF NOT EXISTS arm_ic_series (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    regime        TEXT NOT NULL,
    ic            REAL NOT NULL,
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_arm_ic ON arm_ic_series(strategy_name, regime, ts DESC);
CREATE TABLE IF NOT EXISTS trade_outcomes (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                  TEXT NOT NULL,
    strategy                TEXT NOT NULL,
    regime                  TEXT NOT NULL,
    side                    TEXT NOT NULL,
    entry_price             REAL NOT NULL,
    exit_price              REAL NOT NULL,
    pnl_pct                 REAL NOT NULL,
    exit_reason             TEXT NOT NULL,
    entry_overlay_rationale TEXT,
    closed_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_symbol ON trade_outcomes(symbol, closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_arm ON trade_outcomes(strategy, regime, closed_at DESC);
CREATE TABLE IF NOT EXISTS overlay_cache (
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    ts              TEXT NOT NULL,
    result_side     TEXT NOT NULL,
    result_strength REAL NOT NULL,
    result_reason   TEXT,
    PRIMARY KEY (symbol, side)
);
CREATE TABLE IF NOT EXISTS llm_call_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    provider      TEXT NOT NULL,
    call_site     TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    cache_hit     INTEGER NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    est_cost_usd  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_ts ON llm_call_log(ts);
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
            self._migrate_orders_columns(conn)
            self._migrate_bandit_columns(conn)
            self._migrate_position_owners_pool(conn)

    def _migrate_orders_columns(self, conn: sqlite3.Connection) -> None:
        # CREATE TABLE IF NOT EXISTS only catches brand-new DBs; an existing
        # orders table from before strategy_name/regime were added needs ALTER TABLE.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
        if "strategy_name" not in existing:
            conn.execute("ALTER TABLE orders ADD COLUMN strategy_name TEXT")
        if "regime" not in existing:
            conn.execute("ALTER TABLE orders ADD COLUMN regime TEXT")
        if "signal_strength" not in existing:
            conn.execute("ALTER TABLE orders ADD COLUMN signal_strength REAL")
        if "entry_rationale" not in existing:
            conn.execute("ALTER TABLE orders ADD COLUMN entry_rationale TEXT")

    def _migrate_bandit_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(bandit_weights)")}
        if "alpha_wins" not in existing:
            conn.execute("ALTER TABLE bandit_weights ADD COLUMN alpha_wins INTEGER NOT NULL DEFAULT 1")
        if "beta_losses" not in existing:
            conn.execute("ALTER TABLE bandit_weights ADD COLUMN beta_losses INTEGER NOT NULL DEFAULT 1")

    def _migrate_position_owners_pool(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(position_owners)")}
        if "pool" not in existing:
            conn.execute("ALTER TABLE position_owners ADD COLUMN pool TEXT NOT NULL DEFAULT 'daily'")

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
                "(client_order_id, ts, symbol, side, notional, status, broker_order_id, "
                "strategy_name, regime, signal_strength, entry_rationale) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(client_order_id) DO UPDATE SET "
                "status=excluded.status, "
                "broker_order_id=COALESCE(excluded.broker_order_id, orders.broker_order_id)",
                (order.client_order_id, _now(), order.symbol, order.side,
                 order.notional, order.status, order.broker_order_id,
                 order.strategy_name, order.regime, order.signal_strength,
                 order.entry_rationale),
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

    def try_approve_proposal(self, proposal_id: int) -> dict | None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE proposals SET status=?, decided_at=? WHERE id=? AND status=?",
                (PROPOSAL_APPROVED, _now(), proposal_id, PROPOSAL_PENDING),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            return dict(row) if row else None

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

    def get_last_buy_order(self, symbol: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE symbol=? AND side='buy' ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            return dict(row) if row else None

    def record_trade_outcome(self, outcome: TradeOutcomeRow) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO trade_outcomes "
                "(symbol, strategy, regime, side, entry_price, exit_price, pnl_pct, "
                "exit_reason, entry_overlay_rationale, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (outcome.symbol, outcome.strategy, outcome.regime, outcome.side,
                 outcome.entry_price, outcome.exit_price, outcome.pnl_pct,
                 outcome.exit_reason, outcome.entry_overlay_rationale, outcome.closed_at),
            )
            return int(cur.lastrowid)

    def get_recent_outcomes(
        self,
        symbol: str | None = None,
        strategy: str | None = None,
        regime: str | None = None,
        limit: int = 3,
    ) -> list[dict]:
        clauses = []
        params: list = []
        if symbol is not None:
            clauses.append("symbol=?")
            params.append(symbol)
        if strategy is not None:
            clauses.append("strategy=?")
            params.append(strategy)
        if regime is not None:
            clauses.append("regime=?")
            params.append(regime)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM trade_outcomes {where} ORDER BY closed_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_runs(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, started_at, strategy, mode, note FROM runs ORDER BY id DESC LIMIT 20"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_strategy_signal_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.strategy, COUNT(*) AS cnt "
                "FROM signals s JOIN runs r ON s.run_id = r.id "
                "WHERE r.mode = 'auto' GROUP BY r.strategy"
            ).fetchall()
            return {row["strategy"]: row["cnt"] for row in rows}

    def get_position_owners(self) -> dict[tuple[str, str], str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT symbol, pool, strategy FROM position_owners").fetchall()
            return {(row["symbol"], row["pool"]): row["strategy"] for row in rows}

    def set_position_owner(self, symbol: str, strategy: str, pool: str = "daily") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO position_owners (symbol, pool, strategy, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(symbol, pool) DO UPDATE SET strategy=excluded.strategy, updated_at=excluded.updated_at",
                (symbol, pool, strategy, _now()),
            )

    def clear_position_owner(self, symbol: str, pool: str = "daily") -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM position_owners WHERE symbol=? AND pool=?",
                (symbol, pool),
            )

    def get_bandit_weight(self, strategy: str, regime: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT weight FROM bandit_weights WHERE strategy_name=? AND regime=?",
                (strategy, regime),
            ).fetchone()
            return float(row["weight"]) if row else 1.0

    def save_bandit_weight(self, strategy: str, regime: str, weight: float, cycle_index: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO bandit_weights (strategy_name, regime, weight, cycle_index, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(strategy_name, regime) DO UPDATE SET "
                "weight=excluded.weight, cycle_index=excluded.cycle_index, updated_at=excluded.updated_at",
                (strategy, regime, weight, cycle_index, _now()),
            )

    def get_all_bandit_weights(self) -> dict[tuple[str, str], tuple[float, int]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT strategy_name, regime, weight, cycle_index FROM bandit_weights"
            ).fetchall()
            return {(r["strategy_name"], r["regime"]): (float(r["weight"]), int(r["cycle_index"])) for r in rows}

    def save_bandit_arm(self, strategy: str, regime: str,
                        alpha: int, beta: int, cycle_index: int, weight: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO bandit_weights "
                "(strategy_name, regime, weight, cycle_index, updated_at, alpha_wins, beta_losses) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(strategy_name, regime) DO UPDATE SET "
                "weight=excluded.weight, cycle_index=excluded.cycle_index, "
                "updated_at=excluded.updated_at, alpha_wins=excluded.alpha_wins, "
                "beta_losses=excluded.beta_losses",
                (strategy, regime, weight, cycle_index, _now(), alpha, beta),
            )

    def get_all_bandit_arms(self) -> dict[tuple[str, str], tuple[int, int, int]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT strategy_name, regime, alpha_wins, beta_losses, cycle_index "
                "FROM bandit_weights"
            ).fetchall()
            return {
                (r["strategy_name"], r["regime"]): (
                    int(r["alpha_wins"]), int(r["beta_losses"]), int(r["cycle_index"])
                )
                for r in rows
            }

    def append_ic_observation(self, strategy: str, regime: str, ic: float, ts: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO arm_ic_series (strategy_name, regime, ic, ts) VALUES (?, ?, ?, ?)",
                (strategy, regime, ic, ts),
            )

    def get_ic_series(self, strategy: str, regime: str, limit: int = 60) -> list[float]:
        # ORDER BY ts works correctly only when ts values are full ISO-8601 UTC strings
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ic FROM arm_ic_series WHERE strategy_name=? AND regime=? "
                "ORDER BY ts DESC LIMIT ?",
                (strategy, regime, limit),
            ).fetchall()
        return [float(r["ic"]) for r in reversed(rows)]  # oldest-first

    def get_overlay_cache(self, symbol: str, side: str, ttl_seconds: float) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ts, result_side, result_strength, result_reason "
                "FROM overlay_cache WHERE symbol=? AND side=?",
                (symbol, side),
            ).fetchone()
        if row is None:
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["ts"])).total_seconds()
        if age >= ttl_seconds:
            return None
        return {
            "side": row["result_side"],
            "strength": float(row["result_strength"]),
            "reason": row["result_reason"],
        }

    def set_overlay_cache(self, symbol: str, side: str, result_side: str,
                          result_strength: float, result_reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO overlay_cache (symbol, side, ts, result_side, result_strength, result_reason) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (symbol, side) DO UPDATE SET "
                "ts=excluded.ts, result_side=excluded.result_side, "
                "result_strength=excluded.result_strength, result_reason=excluded.result_reason",
                (symbol, side, datetime.now(timezone.utc).isoformat(), result_side, result_strength, result_reason),
            )

    def record_llm_call(
        self,
        provider: str,
        call_site: str,
        symbol: str,
        cache_hit: bool,
        input_tokens: int,
        output_tokens: int,
        est_cost_usd: float,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO llm_call_log "
                "(ts, provider, call_site, symbol, cache_hit, input_tokens, output_tokens, est_cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), provider, call_site, symbol,
                 cache_hit, input_tokens, output_tokens, est_cost_usd),
            )
