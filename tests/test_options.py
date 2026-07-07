"""Options trading: broker contract selection, risk-gate combined cap, Wheel state
machine, and repository CRUD. No network/Alpaca keys — everything is fake-injected."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from trader.config import RiskLimits
from trader.execution.options_broker import AlpacaOptionsBroker, ContractCandidate
from trader.pipeline import _execute_csp_entry
from trader.portfolio.repository import OptionsPositionRow
from trader.portfolio.sqlite_repo import SQLiteRepository
from trader.risk.gate import AccountState, KillSwitch, OptionsOrderIntent, RiskGate
from trader.strategy.base import Signal
from trader.strategy.dip_recovery import DipRecovery
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


def test_select_csp_contract_boundary_exact_budget_match_is_accepted():
    # strike*100 == max_collateral exactly — the "<=" boundary must be inclusive.
    contracts = [_FakeContract("AAPL_130P", 130.0, date(2026, 1, 16), 500)]
    limits = RiskLimits(options_min_open_interest=100)
    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": limits})(), client=_FakeTradingClient(contracts),
    )
    picked = broker.select_csp_contract("AAPL", ref_price=150.0, max_collateral=13_000.0)
    assert picked.symbol == "AAPL_130P"


def test_select_csp_contract_tie_break_picks_a_max_strike_candidate():
    # Two contracts tie on the max strike — max() must resolve deterministically to one
    # of the tied candidates rather than erroring or picking a lower strike.
    contracts = [
        _FakeContract("AAPL_130P_A", 130.0, date(2026, 1, 16), 500),
        _FakeContract("AAPL_130P_B", 130.0, date(2026, 2, 20), 500),
    ]
    limits = RiskLimits(options_min_open_interest=100)
    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": limits})(), client=_FakeTradingClient(contracts),
    )
    picked = broker.select_csp_contract("AAPL", ref_price=150.0, max_collateral=20_000.0)
    assert picked.symbol in {"AAPL_130P_A", "AAPL_130P_B"}
    assert picked.strike == 130.0


# ---- order submission (idempotency) ----

class _DuplicateOrderTradingClient(_FakeTradingClient):
    """Simulates Alpaca rejecting a retried submit_order because the client_order_id
    already exists — the real duplicate-order error shape from the API."""

    def __init__(self):
        super().__init__(contracts=[])
        self.submit_calls = 0

    def submit_order(self, order_data):
        self.submit_calls += 1
        raise Exception("client_order_id already exists (duplicate)")

    def get_order_by_client_id(self, client_id):
        return type("O", (), {"id": "existing-order-id", "client_order_id": client_id})()


def test_sell_to_open_retry_returns_existing_order_on_duplicate_client_order_id():
    client = _DuplicateOrderTradingClient()
    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": RiskLimits()})(), client=client,
    )
    order = broker.sell_to_open(contract_symbol="AAPL_TESTPUT", client_order_id="dup-id")
    assert order.id == "existing-order-id"
    assert client.submit_calls == 1  # no double-fire: exactly one submit attempt


def test_sell_to_open_reraises_non_duplicate_errors():
    class _FailingClient(_FakeTradingClient):
        def submit_order(self, order_data):
            raise Exception("insufficient buying power")

    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": RiskLimits()})(), client=_FailingClient(contracts=[]),
    )
    with pytest.raises(Exception, match="insufficient buying power"):
        broker.sell_to_open(contract_symbol="AAPL_TESTPUT", client_order_id="oid1")


def test_buy_to_close_submits_buy_side_request():
    captured = {}

    class _CapturingClient(_FakeTradingClient):
        def submit_order(self, order_data):
            captured["symbol"] = order_data.symbol
            captured["side"] = order_data.side
            captured["position_intent"] = order_data.position_intent
            return type("O", (), {"id": "order-id"})()

    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": RiskLimits()})(), client=_CapturingClient(contracts=[]),
    )
    broker.buy_to_close(contract_symbol="AAPL_TESTPUT", client_order_id="oid1")
    assert captured["symbol"] == "AAPL_TESTPUT"
    assert str(captured["side"]).endswith("BUY")
    assert str(captured["position_intent"]).endswith("BUY_TO_CLOSE")


# ---- check_spread ----

class _FakeQuote:
    def __init__(self, bid, ask):
        self.bid_price, self.ask_price = bid, ask


class _FakeDataClient:
    def __init__(self, quotes: dict):
        self._quotes = quotes

    def get_option_latest_quote(self, request):
        return self._quotes


def test_check_spread_computes_fraction_of_mid():
    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": RiskLimits()})(),
        client=_FakeTradingClient(contracts=[]),
        data_client=_FakeDataClient({"AAPL_P": _FakeQuote(bid=9.0, ask=11.0)}),
    )
    spread = broker.check_spread("AAPL_P")
    assert spread == pytest.approx(0.2)  # (11-9) / ((11+9)/2)


def test_check_spread_returns_none_on_non_positive_quote():
    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": RiskLimits()})(),
        client=_FakeTradingClient(contracts=[]),
        data_client=_FakeDataClient({"AAPL_P": _FakeQuote(bid=0.0, ask=11.0)}),
    )
    assert broker.check_spread("AAPL_P") is None


def test_check_spread_returns_none_on_exception():
    class _FailingDataClient:
        def get_option_latest_quote(self, request):
            raise Exception("quote unavailable")

    broker = AlpacaOptionsBroker(
        config=type("C", (), {"risk": RiskLimits()})(),
        client=_FakeTradingClient(contracts=[]),
        data_client=_FailingDataClient(),
    )
    assert broker.check_spread("AAPL_P") is None


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
def sqlite_repo(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "test_options.db"))
    yield repo


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


def test_options_position_insert_idempotent_on_opening_order_id(sqlite_repo):
    row = OptionsPositionRow(
        contract_symbol="AAPL260116P00150000", underlying="AAPL", option_type="put",
        strike=150.0, expiry="2026-01-16", opening_order_id="abc123",
        strategy="csp_on_dip", collateral=15_000.0,
    )
    id1 = sqlite_repo.record_options_position(row)
    id2 = sqlite_repo.record_options_position(row)
    assert id1 == id2
    assert len(sqlite_repo.get_open_options_positions("AAPL")) == 1


def test_options_position_same_contract_symbol_reopens_across_wheel_cycles(sqlite_repo):
    """contract_symbol (the OCC symbol) is not the uniqueness key — the same strike/expiry
    can legitimately be sold again in a later Wheel cycle after the first position on it
    closed. Uniqueness lives on opening_order_id (one row per broker order)."""
    first = OptionsPositionRow(
        contract_symbol="AAPL260116P00150000", underlying="AAPL", option_type="put",
        strike=150.0, expiry="2026-01-16", opening_order_id="oid_cycle1",
        strategy="wheel", collateral=15_000.0,
    )
    id1 = sqlite_repo.record_options_position(first)
    sqlite_repo.update_options_position("AAPL260116P00150000", wheel_state="csp_expired", status="closed")

    second = OptionsPositionRow(
        contract_symbol="AAPL260116P00150000", underlying="AAPL", option_type="put",
        strike=150.0, expiry="2026-01-16", opening_order_id="oid_cycle2",
        strategy="wheel", collateral=15_000.0,
    )
    id2 = sqlite_repo.record_options_position(second)
    assert id2 != id1  # a genuinely new row, not the stale closed one
    open_positions = sqlite_repo.get_open_options_positions("AAPL")
    assert len(open_positions) == 1
    assert open_positions[0]["opening_order_id"] == "oid_cycle2"


def test_update_options_position_does_not_touch_stale_closed_row_sharing_symbol(sqlite_repo):
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_P", underlying="AAPL", option_type="put", strike=140.0,
        expiry="2026-01-16", opening_order_id="oid_old", strategy="wheel", collateral=14_000.0,
        wheel_state="csp_expired", status="closed",
    ))
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_P", underlying="AAPL", option_type="put", strike=140.0,
        expiry="2026-01-16", opening_order_id="oid_new", strategy="wheel", collateral=14_000.0,
    ))
    sqlite_repo.update_options_position("AAPL_P", wheel_state="assigned", status="open", collateral=0.0)
    open_positions = sqlite_repo.get_open_options_positions("AAPL")
    assert len(open_positions) == 1
    assert open_positions[0]["opening_order_id"] == "oid_new"
    assert open_positions[0]["wheel_state"] == "assigned"


def test_total_options_collateral_sums_open_positions(sqlite_repo):
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_P", underlying="AAPL", option_type="put", strike=140.0,
        expiry="2026-01-16", opening_order_id="oid1", strategy="csp_on_dip", collateral=14_000.0,
    ))
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="MSFT_P", underlying="MSFT", option_type="put", strike=300.0,
        expiry="2026-01-16", opening_order_id="oid2", strategy="csp_on_dip", collateral=30_000.0,
    ))
    assert sqlite_repo.get_total_options_collateral() == 44_000.0
    sqlite_repo.update_options_position("AAPL_P", wheel_state="csp_expired", status="closed")
    assert sqlite_repo.get_total_options_collateral() == 30_000.0


def test_update_options_position_rejects_inconsistent_wheel_state_and_status(sqlite_repo):
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_P", underlying="AAPL", option_type="put", strike=140.0,
        expiry="2026-01-16", opening_order_id="oid1", strategy="csp_on_dip", collateral=14_000.0,
    ))
    with pytest.raises(ValueError):
        sqlite_repo.update_options_position("AAPL_P", wheel_state="csp_expired", status="open")
    with pytest.raises(ValueError):
        sqlite_repo.update_options_position("AAPL_P", wheel_state="assigned", status="closed")


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
    def __init__(self, cc_contract=None, csp_contract=None):
        self._cc_contract = cc_contract
        self._csp_contract = csp_contract

    def open_option_positions(self):
        return []  # nothing at the broker — everything expired/settled

    def select_cc_contract(self, underlying, ref_price, shares_held):
        return self._cc_contract

    def select_csp_contract(self, underlying, ref_price, budget):
        return self._csp_contract

    def check_spread(self, contract_symbol):
        return 0.01

    def sell_to_open(self, *, contract_symbol, client_order_id):
        return type("O", (), {"id": "order-id"})()


def test_reconcile_options_marks_assigned_then_wheel_sells_cc(sqlite_repo, tmp_path):
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
    # collateral zeroed on assignment — the cash was spent buying shares, so it must
    # not still count as options exposure (would double-count against stock exposure).
    assert positions[0]["collateral"] == 0.0
    assert sqlite_repo.get_total_options_collateral() == 0.0

    options_broker = _FakeOptionsBroker(
        cc_contract=ContractCandidate("AAPL_TESTCALL", 150.0, date.today() + timedelta(days=30), 200)
    )
    gate = RiskGate(RiskLimits(max_options_allocation_pct=0.5, options_max_spread_pct=0.1))
    ks = KillSwitch(tmp_path / "kill_switch.flag")  # file does not exist → disengaged
    config = type("C", (), {
        "risk": RiskLimits(max_options_allocation_pct=0.5, options_max_spread_pct=0.1),
    })()
    acted = run_wheel_tick(config, options_broker, stock_broker, sqlite_repo, gate, ks, ["AAPL"])

    assert acted == ["AAPL"]
    positions = sqlite_repo.get_open_options_positions("AAPL")
    cc_rows = [p for p in positions if p["wheel_state"] == "cc_open"]
    assert len(cc_rows) == 1
    assert cc_rows[0]["contract_symbol"] == "AAPL_TESTCALL"


def test_run_wheel_tick_restarts_csp_after_called_away(sqlite_repo, tmp_path):
    """Covered call's shares are gone (called away) while its row is still open —
    advance_wheel_state's sell_csp branch should restart the wheel with a new CSP."""
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_TESTCALL", underlying="AAPL", option_type="call", strike=150.0,
        expiry=(date.today() + timedelta(days=30)).isoformat(), opening_order_id="oid_cc",
        strategy="wheel", collateral=15_000.0, wheel_state="cc_open", status="open",
    ))
    stock_broker = _FakeStockBroker({}, {"AAPL": 140.0})  # 0 shares held — called away
    options_broker = _FakeOptionsBroker(
        csp_contract=ContractCandidate("AAPL_TESTPUT2", 135.0, date.today() + timedelta(days=30), 200)
    )
    gate = RiskGate(RiskLimits(max_options_allocation_pct=0.5, options_max_spread_pct=0.1))
    ks = KillSwitch(tmp_path / "kill_switch.flag")
    config = type("C", (), {
        "risk": RiskLimits(max_options_allocation_pct=0.5, options_max_spread_pct=0.1),
    })()

    acted = run_wheel_tick(config, options_broker, stock_broker, sqlite_repo, gate, ks, ["AAPL"])

    assert acted == ["AAPL"]
    positions = sqlite_repo.get_open_options_positions("AAPL")
    csp_rows = [p for p in positions if p["wheel_state"] == "csp_open"]
    assert len(csp_rows) == 1
    assert csp_rows[0]["contract_symbol"] == "AAPL_TESTPUT2"


def test_reconcile_options_csp_expires_worthless(sqlite_repo):
    past_expiry = (date.today() - timedelta(days=1)).isoformat()
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_TESTPUT", underlying="AAPL", option_type="put", strike=140.0,
        expiry=past_expiry, opening_order_id="oid1", strategy="csp_on_dip",
        collateral=14_000.0, wheel_state="csp_open", status="open",
    ))
    stock_broker = _FakeStockBroker({}, {})  # no shares — put expired worthless
    reconcile_options(_FakeOptionsBroker(), stock_broker, sqlite_repo)
    assert sqlite_repo.get_open_options_positions("AAPL") == []


def test_reconcile_options_called_away(sqlite_repo):
    past_expiry = (date.today() - timedelta(days=1)).isoformat()
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_TESTCALL", underlying="AAPL", option_type="call", strike=150.0,
        expiry=past_expiry, opening_order_id="oid1", strategy="wheel",
        collateral=15_000.0, wheel_state="cc_open", status="open",
    ))
    stock_broker = _FakeStockBroker({}, {})  # shares gone — call was exercised
    reconcile_options(_FakeOptionsBroker(), stock_broker, sqlite_repo)
    assert sqlite_repo.get_open_options_positions("AAPL") == []


def test_reconcile_options_cc_expires_worthless(sqlite_repo):
    past_expiry = (date.today() - timedelta(days=1)).isoformat()
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_TESTCALL", underlying="AAPL", option_type="call", strike=150.0,
        expiry=past_expiry, opening_order_id="oid1", strategy="wheel",
        collateral=15_000.0, wheel_state="cc_open", status="open",
    ))
    stock_broker = _FakeStockBroker({"AAPL": 100.0}, {"AAPL": 140.0})  # shares still held
    reconcile_options(_FakeOptionsBroker(), stock_broker, sqlite_repo)
    assert sqlite_repo.get_open_options_positions("AAPL") == []


def test_reconcile_options_skips_position_broker_still_reports_open(sqlite_repo):
    past_expiry = (date.today() - timedelta(days=1)).isoformat()
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_TESTPUT", underlying="AAPL", option_type="put", strike=140.0,
        expiry=past_expiry, opening_order_id="oid1", strategy="csp_on_dip",
        collateral=14_000.0, wheel_state="csp_open", status="open",
    ))
    stock_broker = _FakeStockBroker({}, {})

    class _StaleBroker(_FakeOptionsBroker):
        def open_option_positions(self):
            return [{"symbol": "AAPL_TESTPUT"}]

    reconcile_options(_StaleBroker(), stock_broker, sqlite_repo)
    positions = sqlite_repo.get_open_options_positions("AAPL")
    assert len(positions) == 1
    assert positions[0]["wheel_state"] == "csp_open"


# ---- duplicate CSP entry guard ----

def test_csp_entry_blocked_when_underlying_already_has_open_position(sqlite_repo, tmp_path):
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_TESTPUT", underlying="AAPL", option_type="put", strike=140.0,
        expiry=(date.today() + timedelta(days=30)).isoformat(), opening_order_id="oid1",
        strategy="wheel", collateral=14_000.0, wheel_state="csp_open", status="open",
    ))
    signal = Signal(symbol="AAPL", side="buy", strength=1.0, reason="dip")
    config = type("C", (), {
        "autonomy": "auto",
        "risk": RiskLimits(max_options_allocation_pct=0.5, options_max_spread_pct=0.1),
    })()
    gate = RiskGate(config.risk)
    ks = KillSwitch(tmp_path / "kill_switch.flag")
    state = AccountState(
        equity=100_000, positions={}, open_order_symbols=frozenset(),
        trades_today=0, daily_pnl_pct=0.0, cash=50_000,
    )
    result = _execute_csp_entry(
        signal=signal, run_id=1, strategy=DipRecovery("AAPL"), config=config,
        options_broker=_FakeOptionsBroker(), repo=sqlite_repo, gate=gate,
        kill_switch=ks, state=state, asof=None, ref_price=150.0,
    )
    assert result.outcome == "blocked"
    assert "already has an open" in result.risk_decision.reason
    # only the original position — no second CSP was sold
    assert len(sqlite_repo.get_open_options_positions("AAPL")) == 1


def test_reconcile_options_noop_when_stock_state_stale(sqlite_repo):
    sqlite_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_TESTPUT", underlying="AAPL", option_type="put", strike=140.0,
        expiry=(date.today() - timedelta(days=1)).isoformat(), opening_order_id="oid1",
        strategy="csp_on_dip", collateral=14_000.0, wheel_state="csp_open", status="open",
    ))

    class _StaleStockBroker:
        def reconcile(self):
            return AccountState(
                equity=0, positions={}, open_order_symbols=frozenset(),
                trades_today=0, daily_pnl_pct=None, stale=True,
            )

    reconcile_options(_FakeOptionsBroker(), _StaleStockBroker(), sqlite_repo)
    positions = sqlite_repo.get_open_options_positions("AAPL")
    assert positions[0]["wheel_state"] == "csp_open"
