"""Postgres implementation of PortfolioRepository.

Activated when DATABASE_URL is set in the environment. Mirrors SQLiteRepository
exactly — same method signatures, same short-lived connection pattern, same
idempotent schema init. Uses psycopg2 (sync) to match the blocking call sites.
"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

from trader.portfolio.repository import (
    PROPOSAL_PENDING,
    DecisionFeaturesRow,
    OptionsPositionRow,
    OrderRow,
    PortfolioRepository,
    ProposalRow,
    SignalRow,
    TradeOutcomeRow,
    validate_options_transition,
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
    symbol     TEXT NOT NULL,
    pool       VARCHAR(10) NOT NULL DEFAULT 'daily',
    strategy   TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, pool)
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
CREATE TABLE IF NOT EXISTS trade_outcomes (
    id                      SERIAL PRIMARY KEY,
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
CREATE TABLE IF NOT EXISTS options_positions (
    id               SERIAL PRIMARY KEY,
    contract_symbol  TEXT NOT NULL,
    underlying       TEXT NOT NULL,
    option_type      TEXT NOT NULL,
    strike           REAL NOT NULL,
    expiry           TEXT NOT NULL,
    opening_order_id TEXT NOT NULL UNIQUE,
    strategy         TEXT NOT NULL,
    collateral       REAL NOT NULL,
    wheel_state      TEXT NOT NULL DEFAULT 'csp_open'
        CHECK (wheel_state IN ('csp_open', 'assigned', 'cc_open', 'called_away', 'csp_expired', 'cc_expired')),
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_options_positions_underlying ON options_positions(underlying, status);
CREATE INDEX IF NOT EXISTS idx_options_positions_contract_symbol ON options_positions(contract_symbol);
CREATE TABLE IF NOT EXISTS llm_call_log (
    id            SERIAL PRIMARY KEY,
    ts            TEXT NOT NULL,
    provider      TEXT NOT NULL,
    call_site     TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    cache_hit     BOOLEAN NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    est_cost_usd  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_ts ON llm_call_log(ts);
CREATE TABLE IF NOT EXISTS decision_features (
    id                          SERIAL PRIMARY KEY,
    run_id                      INTEGER NOT NULL,
    ts                          TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    side                        TEXT NOT NULL,
    strategy                    TEXT NOT NULL,
    regime                      TEXT NOT NULL,
    mode                        TEXT NOT NULL DEFAULT 'auto',
    signal_strength_pre_overlay REAL NOT NULL,
    features                    TEXT NOT NULL,
    order_id                    INTEGER,
    backfilled                  BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_decision_features_symbol_ts ON decision_features(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_decision_features_order_id ON decision_features(order_id);
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
                cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS entry_rationale TEXT")
                cur.execute("ALTER TABLE bandit_weights ADD COLUMN IF NOT EXISTS alpha_wins INTEGER NOT NULL DEFAULT 1")
                cur.execute("ALTER TABLE bandit_weights ADD COLUMN IF NOT EXISTS beta_losses INTEGER NOT NULL DEFAULT 1")
                cur.execute("ALTER TABLE position_owners ADD COLUMN IF NOT EXISTS pool VARCHAR(10) NOT NULL DEFAULT 'daily'")

                self._migrate_position_owners_pool_pk(cur)

    @staticmethod
    def _migrate_position_owners_pool_pk(cur) -> None:
        """Migrate old single-column PK on position_owners(symbol) -> (symbol, pool).

        Existing databases created before the composite-key fix have the old
        PRIMARY KEY (symbol) even though the `pool` column was added later.
        `set_position_owner` relies on `ON CONFLICT (symbol, pool)`, so we must
        drop the old PK and add the composite one. Deduplication keeps the row
        with the most recent updated_at for each (symbol, pool) pair.
        """
        cur.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'position_owners'::regclass AND i.indisprimary
            """
        )
        pk_columns = {row["attname"] for row in cur.fetchall()}
        # Already migrated (or brand-new table with the composite key).
        if "pool" in pk_columns or not pk_columns:
            return

        # Backfill any rows that existed before the NOT NULL DEFAULT was added.
        cur.execute("UPDATE position_owners SET pool = 'daily' WHERE pool IS NULL")

        # Deduplicate so the new composite primary key can be applied.
        # Keep the most recent updated_at per (symbol, pool); tie-break on ctid.
        cur.execute(
            """
            DELETE FROM position_owners
            WHERE ctid NOT IN (
                SELECT DISTINCT ON (symbol, pool) ctid
                FROM position_owners
                ORDER BY symbol, pool, updated_at DESC, ctid DESC
            )
            """
        )

        # Drop the old single-column primary key and add the composite key.
        cur.execute("ALTER TABLE position_owners DROP CONSTRAINT position_owners_pkey")
        cur.execute("ALTER TABLE position_owners ADD PRIMARY KEY (symbol, pool)")

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
                    "strategy_name, regime, signal_strength, entry_rationale, fill_price) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (client_order_id) DO UPDATE SET "
                    "status=EXCLUDED.status, "
                    "broker_order_id=COALESCE(EXCLUDED.broker_order_id, orders.broker_order_id), "
                    "fill_price=COALESCE(EXCLUDED.fill_price, orders.fill_price) "
                    "RETURNING id",
                    (order.client_order_id, _now(), order.symbol, order.side,
                     order.notional, order.status, order.broker_order_id,
                     order.strategy_name, order.regime, order.signal_strength,
                     order.entry_rationale, order.fill_price),
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

    def get_last_buy_order(self, symbol: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM orders WHERE symbol=%s AND side='buy' "
                    "ORDER BY id DESC LIMIT 1",
                    (symbol,),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def get_orders_by_status(self, status: str, since_ts: str) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM orders WHERE status=%s AND ts>=%s ORDER BY id",
                    (status, since_ts),
                )
                return [dict(r) for r in cur.fetchall()]

    def get_pending_sell_order(self, symbol: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM orders WHERE symbol=%s AND side='sell' "
                    "AND status='submitted' ORDER BY id DESC LIMIT 1",
                    (symbol,),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def get_highest_buy_price(self, symbol: str) -> float | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(fill_price) AS highest FROM orders "
                    "WHERE symbol=%s AND side='buy' AND status='filled' "
                    "AND fill_price IS NOT NULL AND ts >= COALESCE("
                    "  (SELECT MAX(ts) FROM orders "
                    "   WHERE symbol=%s AND side='sell' AND status='filled'),"
                    "  '-infinity'"
                    ")",
                    (symbol, symbol),
                )
                row = cur.fetchone()
                return float(row["highest"]) if row and row["highest"] is not None else None

    def record_trade_outcome(self, outcome: TradeOutcomeRow) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO trade_outcomes "
                    "(symbol, strategy, regime, side, entry_price, exit_price, pnl_pct, "
                    "exit_reason, entry_overlay_rationale, closed_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (outcome.symbol, outcome.strategy, outcome.regime, outcome.side,
                     outcome.entry_price, outcome.exit_price, outcome.pnl_pct,
                     outcome.exit_reason, outcome.entry_overlay_rationale, outcome.closed_at),
                )
                return int(cur.fetchone()["id"])

    def record_decision_features(self, row: DecisionFeaturesRow) -> int:
        import json
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO decision_features "
                    "(run_id, ts, symbol, side, strategy, regime, mode, "
                    "signal_strength_pre_overlay, features, backfilled) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING id",
                    (row.run_id, _now(), row.symbol, row.side, row.strategy,
                     row.regime, row.mode,
                     row.signal_strength_pre_overlay,
                     json.dumps(row.features, allow_nan=False), row.backfilled),
                )
                return int(cur.fetchone()["id"])

    def link_order_to_decision_features(self, run_id: int, order_id: int) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE decision_features SET order_id = %s "
                    "WHERE run_id = %s AND order_id IS NULL",
                    (order_id, run_id),
                )

    def get_decision_features_by_order_id(self, order_id: int) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM decision_features WHERE order_id = %s", (order_id,)
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def get_decision_features_count(self, linked_only: bool = False) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                if linked_only:
                    cur.execute("SELECT COUNT(*) AS c FROM decision_features WHERE order_id IS NOT NULL")
                else:
                    cur.execute("SELECT COUNT(*) AS c FROM decision_features")
                return int(cur.fetchone()["c"])

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
            clauses.append("symbol=%s")
            params.append(symbol)
        if strategy is not None:
            clauses.append("strategy=%s")
            params.append(strategy)
        if regime is not None:
            clauses.append("regime=%s")
            params.append(regime)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM trade_outcomes {where} ORDER BY closed_at DESC LIMIT %s",
                    params,
                )
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

    def get_overlay_cache(self, symbol: str, side: str, ttl_seconds: float) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, result_side, result_strength, result_reason "
                    "FROM overlay_cache WHERE symbol=%s AND side=%s",
                    (symbol, side),
                )
                row = cur.fetchone()
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
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO overlay_cache (symbol, side, ts, result_side, result_strength, result_reason) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (symbol, side) DO UPDATE SET "
                    "ts=EXCLUDED.ts, result_side=EXCLUDED.result_side, "
                    "result_strength=EXCLUDED.result_strength, result_reason=EXCLUDED.result_reason",
                    (symbol, side, _now(), result_side, result_strength, result_reason),
                )

    def record_options_position(self, position: OptionsPositionRow) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO options_positions "
                    "(contract_symbol, underlying, option_type, strike, expiry, opening_order_id, "
                    "strategy, collateral, wheel_state, status, opened_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (opening_order_id) DO NOTHING",
                    (position.contract_symbol, position.underlying, position.option_type,
                     position.strike, position.expiry, position.opening_order_id,
                     position.strategy, position.collateral, position.wheel_state,
                     position.status, _now()),
                )
                cur.execute(
                    "SELECT id FROM options_positions WHERE opening_order_id=%s",
                    (position.opening_order_id,),
                )
                return int(cur.fetchone()["id"])

    def update_options_position(
        self, contract_symbol: str, *, wheel_state: str | None = None, status: str | None = None,
        collateral: float | None = None,
    ) -> None:
        validate_options_transition(wheel_state, status)
        sets, params = [], []
        if wheel_state is not None:
            sets.append("wheel_state=%s")
            params.append(wheel_state)
        if status is not None:
            sets.append("status=%s")
            params.append(status)
        if collateral is not None:
            sets.append("collateral=%s")
            params.append(collateral)
        if not sets:
            return
        params.append(contract_symbol)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE options_positions SET {', '.join(sets)} WHERE contract_symbol=%s AND status='open'",
                    params,
                )

    def get_open_options_positions(self, underlying: str | None = None) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                if underlying is not None:
                    cur.execute(
                        "SELECT * FROM options_positions WHERE status='open' AND underlying=%s",
                        (underlying,),
                    )
                else:
                    cur.execute("SELECT * FROM options_positions WHERE status='open'")
                return [dict(r) for r in cur.fetchall()]

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
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO llm_call_log "
                    "(ts, provider, call_site, symbol, cache_hit, input_tokens, output_tokens, est_cost_usd) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (_now(), provider, call_site, symbol, cache_hit, input_tokens, output_tokens, est_cost_usd),
                )
