"""Sell-fill confirmation and order-status reconciliation.

Covers the two halves of the slow-fill fix:
  1. The tick path defers outcome recording and owner clearing when a sell's fill
     is not confirmed within the wait_for_fill window.
  2. reconcile_order_statuses later settles stuck 'submitted' rows against broker
     truth, recording the deferred outcome and clearing ownership for late fills.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trader.execution.broker import AlpacaBroker
from trader.pipeline import reconcile_order_statuses
from trader.portfolio.repository import OrderRow
from trader.portfolio.sqlite_repo import SQLiteRepository
from trader.risk.gate import AccountState

from tests.test_pipeline import (
    _SYMBOL,
    _FixedStrategy,
    _broker_for,
    _config,
    _run,
)


def _held_state(entry_price: float = 100.0, qty: float = 10.0) -> AccountState:
    return AccountState(
        equity=100_000.0,
        positions={_SYMBOL: qty},
        open_order_symbols=frozenset(),
        trades_today=0,
        daily_pnl_pct=0.0,
        stale=False,
        cash=50_000.0,
        avg_entry_prices={_SYMBOL: entry_price},
        position_owners={(_SYMBOL, "daily"): "_FixedStrategy"},
    )


# ---- tick path: unconfirmed sell defers outcome + keeps owner ----

def test_unconfirmed_sell_defers_outcome_and_keeps_owner(tmp_path, monkeypatch):
    cfg = _config(tmp_path, autonomy="auto")
    repo = SQLiteRepository(cfg.portfolio_db_path)
    repo.set_position_owner(_SYMBOL, "_FixedStrategy", "daily")

    monkeypatch.setattr(AlpacaBroker, "wait_for_fill", lambda self, coid, **kw: None)
    results, repo, broker = _run([_FixedStrategy(_SYMBOL, "sell")], cfg, state=_held_state())

    result = results[0]
    assert result.outcome == "executed"
    assert result.fill_confirmed is False
    # No phantom closed trade.
    assert repo.get_recent_outcomes(symbol=_SYMBOL) == []
    # Order stays 'submitted' for reconciliation to settle.
    orders = repo.get_orders()
    assert len(orders) == 1
    assert orders[0]["status"] == "submitted"
    # Owner retained so the strategy can still manage the position.
    assert repo.get_position_owners().get((_SYMBOL, "daily")) == "_FixedStrategy"


def test_confirmed_sell_records_outcome_and_clears_owner(tmp_path):
    cfg = _config(tmp_path, autonomy="auto")
    repo = SQLiteRepository(cfg.portfolio_db_path)
    repo.set_position_owner(_SYMBOL, "_FixedStrategy", "daily")

    results, repo, broker = _run([_FixedStrategy(_SYMBOL, "sell")], cfg, state=_held_state())

    result = results[0]
    assert result.outcome == "executed"
    assert result.fill_confirmed is True
    outcomes = repo.get_recent_outcomes(symbol=_SYMBOL)
    assert len(outcomes) == 1
    assert repo.get_position_owners().get((_SYMBOL, "daily")) is None


# ---- reconciliation job ----

class _ReconBroker:
    """Duck-typed stand-in: reconcile_order_statuses only needs .get_order()."""

    def __init__(self, orders_by_coid: dict) -> None:
        self._orders = orders_by_coid

    def get_order(self, client_order_id: str):
        return self._orders.get(client_order_id)


def _repo_with_submitted(tmp_path, side: str, coid: str = "sell-1") -> SQLiteRepository:
    repo = SQLiteRepository(str(tmp_path / "recon.db"))
    repo.record_order(OrderRow(
        client_order_id=coid, symbol=_SYMBOL, side=side, notional=1000.0,
        status="submitted", strategy_name="_FixedStrategy", regime="normal",
    ))
    return repo


def test_reconciliation_settles_late_filled_sell(tmp_path):
    repo = _repo_with_submitted(tmp_path, "sell")
    repo.record_order(OrderRow(
        client_order_id="buy-1", symbol=_SYMBOL, side="buy", notional=1000.0,
        status="filled", entry_rationale="dip entry",
    ))
    repo.set_position_owner(_SYMBOL, "_FixedStrategy", "daily")

    broker = _ReconBroker({
        "sell-1": SimpleNamespace(status="filled", filled_avg_price="110.0"),
        "buy-1": SimpleNamespace(status="filled", filled_avg_price="100.0"),
    })
    updated = reconcile_order_statuses(broker, repo)

    assert updated == 1
    statuses = {o["client_order_id"]: o["status"] for o in repo.get_orders()}
    assert statuses["sell-1"] == "filled"
    outcomes = repo.get_recent_outcomes(symbol=_SYMBOL)
    assert len(outcomes) == 1
    assert outcomes[0]["exit_reason"] == "reconciled-exit"
    assert outcomes[0]["pnl_pct"] == pytest.approx(0.10)
    assert repo.get_position_owners().get((_SYMBOL, "daily")) is None


def test_reconciliation_leaves_live_orders_alone(tmp_path):
    repo = _repo_with_submitted(tmp_path, "sell")
    broker = _ReconBroker({"sell-1": SimpleNamespace(status="accepted")})

    assert reconcile_order_statuses(broker, repo) == 0
    assert repo.get_orders()[0]["status"] == "submitted"
    assert repo.get_recent_outcomes(symbol=_SYMBOL) == []


def test_reconciliation_marks_canceled_without_outcome(tmp_path):
    repo = _repo_with_submitted(tmp_path, "buy", coid="buy-2")
    broker = _ReconBroker({"buy-2": SimpleNamespace(status="canceled")})

    assert reconcile_order_statuses(broker, repo) == 1
    assert repo.get_orders()[0]["status"] == "canceled"
    assert repo.get_recent_outcomes(symbol=_SYMBOL) == []


def test_reconciliation_skips_broker_lookup_failures(tmp_path):
    repo = _repo_with_submitted(tmp_path, "sell")
    broker = _ReconBroker({})  # lookup returns None

    assert reconcile_order_statuses(broker, repo) == 0
    assert repo.get_orders()[0]["status"] == "submitted"
