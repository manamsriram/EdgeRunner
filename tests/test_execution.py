"""Execution-adapter tests: idempotency, decision-identity ids, fail-closed reconcile.

A fake TradingClient is injected, so no alpaca-py and no network are involved.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from trader.config import Config, RiskLimits
from trader.execution.broker import AlpacaBroker, client_order_id_for


def _config() -> Config:
    return Config(
        alpaca_api_key="k", alpaca_secret_key="s", alpaca_paper=True, autonomy="manual",
        openai_api_key=None, anthropic_api_key=None,
        portfolio_db_path=":memory:", kill_switch_path="kill.flag", risk=RiskLimits(),
    )


class FakeDuplicateError(Exception):
    def __init__(self) -> None:
        super().__init__("client_order_id already exists")
        self.status_code = 422


class FakeClient:
    """Minimal stand-in for alpaca-py's TradingClient."""

    def __init__(self, *, account=None, positions=None, orders=None) -> None:
        self.account = account or SimpleNamespace(equity="100000", last_equity="100000")
        self.positions = positions or []
        self.orders = orders or {"open": [], "closed": []}
        self.submitted: list = []
        self._by_coid: dict = {}
        self._raise_dup_after = None

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return self.positions

    def get_orders(self, filter):  # noqa: A002 - mirror alpaca's kw name
        # The injected filter builder returns the literal strings "open"/"closed".
        return self.orders[filter]

    def submit_order(self, order_data):
        coid = order_data["client_order_id"]
        if coid in self._by_coid:
            raise FakeDuplicateError()
        order = SimpleNamespace(**order_data)
        self._by_coid[coid] = order
        self.submitted.append(order)
        return order

    def get_order_by_client_order_id(self, client_order_id):
        return self._by_coid[client_order_id]


def _fake_request_builder(symbol, notional, side, client_order_id):
    return {
        "symbol": symbol,
        "notional": notional,
        "side": side,
        "client_order_id": client_order_id,
    }


def _fake_filter_builder(today):
    return "open", "closed"


def _broker(client) -> AlpacaBroker:
    return AlpacaBroker(
        _config(),
        client=client,
        request_builder=_fake_request_builder,
        order_filter_builder=_fake_filter_builder,
    )


# ---- client_order_id ----

def test_client_order_id_is_decision_identity_not_run():
    a = client_order_id_for(date(2026, 6, 3), "AAPL", "buy", "ma_crossover")
    b = client_order_id_for(date(2026, 6, 3), "AAPL", "buy", "ma_crossover")
    assert a == b  # same decision, different "run" => same id
    c = client_order_id_for(date(2026, 6, 4), "AAPL", "buy", "ma_crossover")
    assert a != c  # different date => different id


# ---- idempotent submit ----

def test_submit_places_notional_order():
    client = FakeClient()
    order = _broker(client).submit(
        symbol="AAPL", side="buy", notional=1234.567, client_order_id="coid1"
    )
    assert len(client.submitted) == 1
    assert order.symbol == "AAPL"
    assert order.notional == 1234.567
    assert order.side == "buy"


def test_duplicate_submit_returns_existing_no_double_fire():
    client = FakeClient()
    broker = _broker(client)
    first = broker.submit(symbol="AAPL", side="buy", notional=1000.0, client_order_id="dup")
    second = broker.submit(symbol="AAPL", side="buy", notional=1000.0, client_order_id="dup")
    assert len(client.submitted) == 1          # only one real order
    assert first.client_order_id == second.client_order_id


def test_submit_rejects_bad_side():
    with pytest.raises(ValueError):
        _broker(FakeClient()).submit(
            symbol="AAPL", side="short", notional=1.0, client_order_id="x"
        )


# ---- reconcile ----

def test_reconcile_maps_account_into_state():
    client = FakeClient(
        account=SimpleNamespace(equity="120000", last_equity="100000"),
        positions=[SimpleNamespace(symbol="AAPL", qty="10")],
        orders={
            "open": [SimpleNamespace(symbol="MSFT")],
            "closed": [
                SimpleNamespace(symbol="AAPL", status="filled"),
                SimpleNamespace(symbol="NVDA", status="canceled"),
            ],
        },
    )
    state = _broker(client).reconcile()
    assert not state.stale
    assert state.equity == 120000.0
    assert state.positions == {"AAPL": 10.0}
    assert state.open_order_symbols == frozenset({"MSFT"})
    assert state.trades_today == 1                       # only the filled one counts
    assert state.daily_pnl_pct == pytest.approx(0.2)     # (120k-100k)/100k


def test_reconcile_fails_closed_on_client_error():
    class Boom(FakeClient):
        def get_account(self):
            raise RuntimeError("alpaca down")

    state = _broker(Boom()).reconcile()
    assert state.stale
    assert state.daily_pnl_pct is None


def test_reconcile_unknown_last_equity_is_none():
    client = FakeClient(account=SimpleNamespace(equity="100000", last_equity="0"))
    state = _broker(client).reconcile()
    assert not state.stale
    assert state.daily_pnl_pct is None
