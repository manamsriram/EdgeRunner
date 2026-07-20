"""SQLite portfolio-repo tests: round-trips, idempotent orders, proposal queue, and the
key safety property — coexisting with the existing app.py `users` table (no destructive
migration).
"""
from __future__ import annotations

import sqlite3

import pytest

from trader.portfolio.repository import (
    PROPOSAL_APPROVED,
    DecisionFeaturesRow,
    OrderRow,
    ProposalRow,
    SignalRow,
    TradeOutcomeRow,
)
from trader.portfolio.sqlite_repo import SQLiteRepository


@pytest.fixture
def repo(tmp_path) -> SQLiteRepository:
    return SQLiteRepository(str(tmp_path / "portfolio.db"))


def test_run_signal_roundtrip(repo):
    run_id = repo.record_run("ma_crossover", "manual", "test")
    assert run_id > 0
    sig_id = repo.record_signal(SignalRow(run_id, "AAPL", "buy", 0.8, "sma cross"))
    assert sig_id > 0


def test_order_idempotent_on_client_order_id(repo):
    first = repo.record_order(OrderRow("coid-1", "AAPL", "buy", 1000.0, "accepted"))
    second = repo.record_order(OrderRow("coid-1", "AAPL", "buy", 1000.0, "accepted"))
    assert first == second                  # same id, not a new row
    assert len(repo.get_orders()) == 1


def test_order_persists_strategy_name_and_regime(repo):
    repo.record_order(OrderRow(
        "coid-2", "AAPL", "buy", 1000.0, "accepted",
        strategy_name="DonchianBreakout", regime="calm",
    ))
    orders = repo.get_orders()
    assert orders[0]["strategy_name"] == "DonchianBreakout"
    assert orders[0]["regime"] == "calm"


def test_order_strategy_name_and_regime_default_to_none(repo):
    repo.record_order(OrderRow("coid-3", "AAPL", "buy", 1000.0, "accepted"))
    orders = repo.get_orders()
    assert orders[0]["strategy_name"] is None
    assert orders[0]["regime"] is None


def test_proposal_queue_lifecycle(repo):
    pid = repo.create_proposal(ProposalRow("MSFT", "buy", 500.0, 400.0, "momentum"))
    pending = repo.list_pending_proposals()
    assert [p["id"] for p in pending] == [pid]
    repo.set_proposal_status(pid, PROPOSAL_APPROVED)
    assert repo.list_pending_proposals() == []


def test_wal_mode_enabled(repo):
    conn = repo._connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_bandit_weight_defaults_to_one_when_not_set(repo):
    assert repo.get_bandit_weight("DonchianBreakout", "calm") == 1.0


def test_save_and_retrieve_bandit_weight(repo):
    repo.save_bandit_weight("DonchianBreakout", "calm", 1.3, cycle_index=5)
    assert repo.get_bandit_weight("DonchianBreakout", "calm") == 1.3


def test_save_bandit_weight_upserts(repo):
    repo.save_bandit_weight("DonchianBreakout", "calm", 1.3, cycle_index=5)
    repo.save_bandit_weight("DonchianBreakout", "calm", 1.1, cycle_index=6)
    assert repo.get_bandit_weight("DonchianBreakout", "calm") == 1.1


def test_get_all_bandit_weights_empty(repo):
    assert repo.get_all_bandit_weights() == {}


def test_get_all_bandit_weights_returns_all(repo):
    repo.save_bandit_weight("DonchianBreakout", "calm", 1.3, cycle_index=1)
    repo.save_bandit_weight("SuperTrend", "trending", 0.8, cycle_index=2)
    weights = repo.get_all_bandit_weights()
    assert weights == {
        ("DonchianBreakout", "calm"): (1.3, 1),
        ("SuperTrend", "trending"): (0.8, 2),
    }


def test_get_last_buy_order_returns_most_recent(repo):
    repo.record_order(OrderRow("c1", "AAPL", "buy", 1000.0, "accepted", entry_rationale="first"))
    repo.record_order(OrderRow("c2", "AAPL", "sell", 1000.0, "accepted"))
    repo.record_order(OrderRow("c3", "AAPL", "buy", 1000.0, "accepted", entry_rationale="second"))
    last_buy = repo.get_last_buy_order("AAPL")
    assert last_buy["entry_rationale"] == "second"


def test_get_last_buy_order_none_when_no_buys(repo):
    assert repo.get_last_buy_order("AAPL") is None


def _outcome(**overrides) -> TradeOutcomeRow:
    base = dict(
        symbol="AAPL", strategy="DipRecovery", regime="calm", side="buy",
        entry_price=100.0, exit_price=95.0, pnl_pct=-0.05, exit_reason="stop-loss",
        entry_overlay_rationale="looked good", closed_at="2026-07-01T00:00:00+00:00",
    )
    base.update(overrides)
    return TradeOutcomeRow(**base)


def test_record_and_retrieve_trade_outcome(repo):
    outcome_id = repo.record_trade_outcome(_outcome())
    assert outcome_id > 0
    rows = repo.get_recent_outcomes(symbol="AAPL")
    assert len(rows) == 1
    assert rows[0]["pnl_pct"] == pytest.approx(-0.05)
    assert rows[0]["exit_reason"] == "stop-loss"


def test_get_recent_outcomes_ordered_most_recent_first(repo):
    repo.record_trade_outcome(_outcome(closed_at="2026-07-01T00:00:00+00:00", pnl_pct=-0.01))
    repo.record_trade_outcome(_outcome(closed_at="2026-07-02T00:00:00+00:00", pnl_pct=-0.02))
    rows = repo.get_recent_outcomes(symbol="AAPL", limit=5)
    assert [r["pnl_pct"] for r in rows] == pytest.approx([-0.02, -0.01])


def test_get_recent_outcomes_filters_by_strategy_and_regime(repo):
    repo.record_trade_outcome(_outcome(strategy="DipRecovery", regime="calm"))
    repo.record_trade_outcome(_outcome(strategy="SuperTrend", regime="trending"))
    rows = repo.get_recent_outcomes(strategy="SuperTrend", regime="trending")
    assert len(rows) == 1
    assert rows[0]["strategy"] == "SuperTrend"


def test_get_recent_outcomes_respects_limit(repo):
    for i in range(5):
        repo.record_trade_outcome(_outcome(closed_at=f"2026-07-0{i+1}T00:00:00+00:00"))
    rows = repo.get_recent_outcomes(symbol="AAPL", limit=2)
    assert len(rows) == 2


def test_order_persists_entry_rationale(repo):
    repo.record_order(OrderRow(
        "c-rationale", "AAPL", "buy", 1000.0, "accepted",
        entry_rationale="[overlay approved] strong momentum",
    ))
    orders = repo.get_orders()
    assert orders[0]["entry_rationale"] == "[overlay approved] strong momentum"


def test_coexists_with_app_users_table_no_destruction(tmp_path):
    """Pre-seed an app.py-style users table in the same file, then point the repo at it.
    The repo must add its tables alongside without touching existing user rows."""
    db = str(tmp_path / "users.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE users (username TEXT PRIMARY KEY, password TEXT, email TEXT, "
        "full_name TEXT, created_at DATETIME)"
    )
    conn.execute(
        "INSERT INTO users VALUES ('sri', 'hash', 'a@b.com', 'Sri', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    repo = SQLiteRepository(db)                       # creates trading tables alongside
    repo.record_order(OrderRow("c1", "AAPL", "buy", 100.0, "accepted"))

    conn = sqlite3.connect(db)
    user = conn.execute("SELECT username FROM users WHERE username='sri'").fetchone()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert user is not None and user[0] == "sri"     # untouched
    assert {"users", "orders", "trades", "proposals"} <= tables


def test_record_decision_features_roundtrip(repo):
    run_id = repo.record_run(strategy="DipRecovery", mode="auto")
    row = DecisionFeaturesRow(
        run_id=run_id, symbol="AAPL", side="buy", strategy="DipRecovery",
        regime="normal", mode="auto", signal_strength_pre_overlay=0.8,
        features={"pe_ttm": 22.5, "vol_10d_annualized": 15.0},
    )
    row_id = repo.record_decision_features(row)
    assert isinstance(row_id, int)


def test_link_order_to_decision_features_backfills_order_id(repo):
    run_id = repo.record_run(strategy="DipRecovery", mode="auto")
    row = DecisionFeaturesRow(
        run_id=run_id, symbol="AAPL", side="buy", strategy="DipRecovery",
        regime="normal", mode="auto", signal_strength_pre_overlay=0.8,
        features={"pe_ttm": 22.5},
    )
    repo.record_decision_features(row)
    repo.link_order_to_decision_features(run_id=run_id, order_id=42)
    linked = repo.get_decision_features_by_order_id(42)
    assert linked is not None
    assert linked["symbol"] == "AAPL"
    assert linked["mode"] == "auto"
