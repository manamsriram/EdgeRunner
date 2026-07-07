"""Options trading: broker contract selection, risk-gate combined cap, Wheel state
machine, and repository CRUD. No network/Alpaca keys — everything is fake-injected."""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

import pytest

from trader.config import RiskLimits
from trader.execution.options_broker import AlpacaOptionsBroker, ContractCandidate
from trader.portfolio.repository import OptionsPositionRow
from trader.portfolio.sqlite_repo import SQLiteRepository
from trader.risk.gate import AccountState, KillSwitch, OptionsOrderIntent, RiskGate
from trader.strategy.wheel import advance_wheel_state, reconcile_options, run_wheel_tick


# ---- options_broker.py ----

class _FakeContract:
    def __init__(self, symbol, strike, expiry, oi):
        self.symbol, self.strike_price, self.expiration_date, self.open_interest = (
            symbol, strike, expiry, oi
        )


class _FakeContractsResponse:
    def __init__(self, contracts):
        self.option_contracts = contracts


class _FakeTradingClient:
    def __init__(self, contracts):
        self._contracts = contracts

    def get_option_contracts(self, request):
        return _FakeContractsResponse(self._contracts)

    def submit_order(self, order_data):
        return type("O", (), {"id": "fake-order-id"})()

    def get_order_by_client_id(self, client_id):
        return type("O", (), {"id": "existing-order-id"})()

    def get_all_positions(self):
        return []


def test_eligible_chain_filters_by_open_interest():
    contracts = [
        _FakeContract("AAPL_LOW_OI", 140.0, date(2026, 1, 16), 50),
        _FakeContract("AAPL_OK", 145.0, date(2026, 1, 16), 500),
    ]
    limits = RiskLimits(options_min_open_interest=100)
    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": limits})(), client=_FakeTradingClient(contracts),
    )
    chain = broker.eligible_chain("AAPL", "put")
    assert [c.symbol for c in chain] == ["AAPL_OK"]


def test_select_csp_contract_respects_collateral_budget():
    contracts = [
        _FakeContract("AAPL_140P", 140.0, date(2026, 1, 16), 500),
        _FakeContract("AAPL_120P", 120.0, date(2026, 1, 16), 500),
    ]
    limits = RiskLimits(options_min_open_interest=100)
    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": limits})(), client=_FakeTradingClient(contracts),
    )
    # 140 strike needs $14,000 collateral — exceeds a $13,000 budget, must fall back to 120.
    picked = broker.select_csp_contract("AAPL", ref_price=150.0, max_collateral=13_000)
    assert picked.symbol == "AAPL_120P"

    # Budget too small for even the cheapest contract.
    assert broker.select_csp_contract("AAPL", ref_price=150.0, max_collateral=1_000) is None


def test_select_cc_contract_requires_100_shares():
    contracts = [_FakeContract("AAPL_160C", 160.0, date(2026, 1, 16), 500)]
    limits = RiskLimits(options_min_open_interest=100)
    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": limits})(), client=_FakeTradingClient(contracts),
    )
    assert broker.select_cc_contract("AAPL", ref_price=150.0, shares_held=50) is None
    picked = broker.select_cc_contract("AAPL", ref_price=150.0, shares_held=100)
    assert picked.symbol == "AAPL_160C"


# ---- risk gate combined cap ----

def test_options_gate_rejects_over_options_only_cap():
    gate = RiskGate(RiskLimits(max_options_allocation_pct=0.15))
    state = AccountState(
        equity=100_000, positions={}, open_order_symbols=frozenset(),
        trades_today=0, daily_pnl_pct=0.0, cash=100_000,
    )
    decision = gate.evaluate_options_order(OptionsOrderIntent("AAPL", 16_000), state)
    assert not decision.approved
    assert "options allocation" in decision.reason


def test_options_gate_rejects_combined_nav_breach():
    gate = RiskGate(RiskLimits(max_options_allocation_pct=0.50))
    state = AccountState(
        equity=100_000, positions={"MSFT": 500}, avg_entry_prices={"MSFT": 190.0},
        open_order_symbols=frozenset(), trades_today=0, daily_pnl_pct=0.0,
        cash=5_000, options_collateral=5_000,
    )
    decision = gate.evaluate_options_order(OptionsOrderIntent("AAPL", 5_000), state)
    assert not decision.approved
    assert "combined exposure" in decision.reason


def test_options_gate_fails_closed_on_stale_state():
    gate = RiskGate(RiskLimits())
    state = AccountState(
        equity=0, positions={}, open_order_symbols=frozenset(),
        trades_today=0, daily_pnl_pct=None, stale=True,
    )
    decision = gate.evaluate_options_order(OptionsOrderIntent("AAPL", 1_000), state)
    assert not decision.approved
    assert "stale" in decision.reason


# ---- Wheel state machine ----

@pytest.mark.parametrize(
    "open_positions,shares_held,expected_action",
    [
        ([], 0, "none"),
        ([], 150, "sell_cc"),  # idle shares after a CC expired worthless
        ([{"wheel_state": "assigned"}], 100, "sell_cc"),
        ([{"wheel_state": "assigned"}], 0, "hold"),  # shares not yet settled
        ([{"wheel_state": "cc_open"}], 100, "hold"),
        ([{"wheel_state": "cc_open"}], 0, "sell_csp"),  # called away
        ([{"wheel_state": "csp_open"}], 0, "hold"),
    ],
)
def test_advance_wheel_state(open_positions, shares_held, expected_action):
    assert advance_wheel_state("AAPL", open_positions, shares_held).action == expected_action


# ---- repository CRUD ----

@pytest.fixture
def sqlite_repo():
    path = tempfile.mktemp(suffix=".db")
    repo = SQLiteRepository(path)
    yield repo
    os.remove(path)


def test_options_position_crud(sqlite_repo):
    row = OptionsPositionRow(
        contract_symbol="AAPL260116P00150000", underlying="AAPL", option_type="put",
        strike=150.0, expiry="2026-01-16", opening_order_id="abc123",
        strategy="csp_on_dip", collateral=15_000.0,
    )
    sqlite_repo.record_options_position(row)
    open_positions = sqlite_repo.get_open_options_positions("AAPL")
    assert len(open_positions) == 1
    assert open_positions[0]["wheel_state"] == "csp_open"

    sqlite_repo.update_options_position("AAPL260116P00150000", wheel_state="assigned")
    open_positions = sqlite_repo.get_open_options_positions("AAPL")
    assert open_positions[0]["wheel_state"] == "assigned"

    sqlite_repo.update_options_position("AAPL260116P00150000", status="closed")
    assert sqlite_repo.get_open_options_positions("AAPL") == []


def test_options_position_insert_idempotent_on_contract_symbol(sqlite_repo):
    row = OptionsPositionRow(
        contract_symbol="AAPL260116P00150000", underlying="AAPL", option_type="put",
        strike=150.0, expiry="2026-01-16", opening_order_id="abc123",
        strategy="csp_on_dip", collateral=15_000.0,
    )
    id1 = sqlite_repo.record_options_position(row)
    id2 = sqlite_repo.record_options_position(row)
    assert id1 == id2
    assert len(sqlite_repo.get_open_options_positions("AAPL")) == 1


# ---- reconcile_options / run_wheel_tick integration ----

class _FakeStockBroker:
    def __init__(self, positions, avg_entry_prices=None):
        self._positions = positions
        self._avg = avg_entry_prices or {}

    def reconcile(self):
        return AccountState(
            equity=100_000, positions=self._positions, avg_entry_prices=self._avg,
            open_order_symbols=frozenset(), trades_today=0, daily_pnl_pct=0.0, cash=50_000,
        )


class _FakeOptionsBroker:
    def __init__(self, cc_contract=None):
        self._cc_contract = cc_contract

    def open_option_positions(self):
        return []  # nothing at the broker — everything expired/settled

    def select_cc_contract(self, underlying, ref_price, shares_held):
        return self._cc_contract

    def check_spread(self, contract_symbol):
        return 0.01

    def sell_to_open(self, *, contract_symbol, client_order_id):
        return type("O", (), {"id": "order-id"})()


def test_reconcile_options_marks_assigned_then_wheel_sells_cc(sqlite_repo):
    past_expiry = (date.today() - timedelta(days=1)).isoformat()
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_TESTPUT", underlying="AAPL", option_type="put", strike=140.0,
        expiry=past_expiry, opening_order_id="oid1", strategy="wheel",
        collateral=14_000.0, wheel_state="csp_open", status="open",
    ))
    stock_broker = _FakeStockBroker({"AAPL": 100.0}, {"AAPL": 140.0})
    options_broker = _FakeOptionsBroker()

    reconcile_options(options_broker, stock_broker, sqlite_repo)
    positions = sqlite_repo.get_open_options_positions("AAPL")
    assert positions[0]["wheel_state"] == "assigned"

    options_broker = _FakeOptionsBroker(
        cc_contract=ContractCandidate("AAPL_TESTCALL", 150.0, date.today() + timedelta(days=30), 200)
    )
    gate = RiskGate(RiskLimits(max_options_allocation_pct=0.5, options_max_spread_pct=0.1))
    ks = KillSwitch(tempfile.mktemp())
    config = type("C", (), {
        "risk": RiskLimits(max_options_allocation_pct=0.5, options_max_spread_pct=0.1),
    })()
    acted = run_wheel_tick(config, options_broker, stock_broker, sqlite_repo, gate, ks, ["AAPL"])

    assert acted == ["AAPL"]
    positions = sqlite_repo.get_open_options_positions("AAPL")
    cc_rows = [p for p in positions if p["wheel_state"] == "cc_open"]
    assert len(cc_rows) == 1
    assert cc_rows[0]["contract_symbol"] == "AAPL_TESTCALL"
