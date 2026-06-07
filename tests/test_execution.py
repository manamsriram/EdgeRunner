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

    def get_order_by_client_id(self, client_id):
        return self._by_coid[client_id]


def _fake_request_builder(*, symbol, side, client_order_id, notional=None, qty=None):
    return {
        "symbol": symbol,
        "notional": notional,
        "qty": qty,
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
    assert order.qty is None
    assert order.side == "buy"


def test_submit_sell_uses_qty_not_notional():
    client = FakeClient()
    order = _broker(client).submit(
        symbol="AAPL", side="sell", qty=10.0, client_order_id="coid-sell"
    )
    assert order.qty == 10.0
    assert order.notional is None


def test_submit_requires_exactly_one_of_notional_or_qty():
    with pytest.raises(ValueError):
        _broker(FakeClient()).submit(symbol="AAPL", side="buy", client_order_id="x")
    with pytest.raises(ValueError):
        _broker(FakeClient()).submit(
            symbol="AAPL", side="buy", notional=1.0, qty=1.0, client_order_id="x"
        )


def test_duplicate_submit_returns_existing_no_double_fire():
    client = FakeClient()
    broker = _broker(client)
    first = broker.submit(symbol="AAPL", side="buy", notional=1000.0, client_order_id="dup")
    second = broker.submit(symbol="AAPL", side="buy", notional=1000.0, client_order_id="dup")
    assert len(client.submitted) == 1          # only one real order
    assert first.client_order_id == second.client_order_id


def test_submit_sell_with_notional_routes_as_notional():
    """sell + notional (no qty) should pass notional through, not set qty."""
    client = FakeClient()
    order = _broker(client).submit(
        symbol="AAPL", side="sell", notional=500.0, client_order_id="sell-notional"
    )
    assert order.notional == 500.0
    assert order.qty is None
    assert order.side == "sell"


class FakeValidationError(Exception):
    """A 422 that is NOT a duplicate client_order_id (e.g. bad symbol)."""
    def __init__(self) -> None:
        super().__init__("invalid symbol")
        self.status_code = 422


def test_non_duplicate_422_is_not_swallowed():
    """A 422 whose message doesn't mention client_order_id must propagate, not be
    misclassified as a duplicate and silently routed to get_order_by_client_id."""
    class RaisingClient(FakeClient):
        def submit_order(self, order_data):
            raise FakeValidationError()

    with pytest.raises(FakeValidationError):
        _broker(RaisingClient()).submit(
            symbol="AAPL", side="buy", notional=100.0, client_order_id="any"
        )


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


# ---- real alpaca-py SDK contract (skipped if the SDK isn't installed) ----
# The fakes above never touch the real SDK, so these guard against signature/enum drift
# in the lazy-import builders and the assumed TradingClient method names.

def test_real_sdk_request_builders_construct():
    pytest.importorskip("alpaca")
    from trader.execution.broker import _build_market_order_request, _build_order_filters

    buy = _build_market_order_request(
        symbol="AAPL", side="buy", client_order_id="c1", notional=50.0
    )
    assert float(buy.notional) == 50.0 and buy.client_order_id == "c1"
    sell = _build_market_order_request(
        symbol="AAPL", side="sell", client_order_id="c2", qty=3.0
    )
    assert float(sell.qty) == 3.0
    open_filter, closed_filter = _build_order_filters(date(2026, 6, 3))
    assert open_filter.status is not None and closed_filter.after is not None


def test_real_trading_client_has_assumed_methods():
    pytest.importorskip("alpaca")
    from alpaca.trading.client import TradingClient

    for method in (
        "get_account", "get_all_positions", "get_orders",
        "submit_order", "get_order_by_client_id", "get_portfolio_history",
    ):
        assert hasattr(TradingClient, method), f"TradingClient missing {method}"


# ---- portfolio history ----

class _PortfolioHistoryClient(FakeClient):
    """FakeClient extended with get_portfolio_history."""

    def __init__(self, *, history=None, raise_exc=None) -> None:
        super().__init__()
        self._history = history
        self._raise_exc = raise_exc

    def get_portfolio_history(self, history_filter=None):
        if self._raise_exc:
            raise self._raise_exc
        return self._history


def test_get_portfolio_history_returns_dict():
    from datetime import datetime as _dt, timezone as _tz
    from types import SimpleNamespace

    ts = [_dt(2026, 6, 1, tzinfo=_tz.utc), _dt(2026, 6, 2, tzinfo=_tz.utc)]
    eq = [100_000.0, 101_500.0]
    fake_history = SimpleNamespace(timestamp=ts, equity=eq)

    client = _PortfolioHistoryClient(history=fake_history)
    result = _broker(client).get_portfolio_history()

    assert result is not None
    assert result["equity"] == eq
    assert len(result["timestamp"]) == 2
    assert "2026-06-01" in result["timestamp"][0]


def test_get_portfolio_history_returns_none_on_error():
    client = _PortfolioHistoryClient(raise_exc=RuntimeError("API down"))
    result = _broker(client).get_portfolio_history()
    assert result is None
