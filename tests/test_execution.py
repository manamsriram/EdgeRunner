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


def test_duplicate_detected_with_alpaca_must_be_unique_message():
    """Production Alpaca rejects duplicate ids with 'client_order_id must be unique'
    (code 40010001). That phrasing must route to the idempotent path, not propagate."""
    class ProdDuplicateClient(FakeClient):
        def submit_order(self, order_data):
            if order_data["client_order_id"] in self._by_coid:
                raise Exception(
                    '{"code":40010001,"message":"client_order_id must be unique"}'
                )
            return super().submit_order(order_data)

    client = ProdDuplicateClient()
    broker = _broker(client)
    first = broker.submit(symbol="SMCI", side="buy", notional=1000.0, client_order_id="dup2")
    second = broker.submit(symbol="SMCI", side="buy", notional=1000.0, client_order_id="dup2")
    assert len(client.submitted) == 1
    assert first.client_order_id == second.client_order_id


# ---- non-fractionable fallback ----

class FakeNotFractionableClient(FakeClient):
    """Rejects notional orders the way Alpaca does for non-fractionable assets."""

    def submit_order(self, order_data):
        if order_data["notional"] is not None:
            raise Exception(
                '{"code":40310000,"message":"asset \\"MSTU\\" is not fractionable"}'
            )
        return super().submit_order(order_data)


def test_notional_buy_falls_back_to_whole_share_qty_when_not_fractionable():
    client = FakeNotFractionableClient()
    order = _broker(client).submit(
        symbol="MSTU", side="buy", notional=1174.13, ref_price=11.50,
        client_order_id="frac1",
    )
    assert len(client.submitted) == 1
    assert order.notional is None
    assert order.qty == 102.0          # floor(1174.13 / 11.50)
    assert order.client_order_id == "frac1"


def test_not_fractionable_fallback_is_idempotent():
    client = FakeNotFractionableClient()
    broker = _broker(client)
    first = broker.submit(
        symbol="MSTU", side="buy", notional=1174.13, ref_price=11.50,
        client_order_id="frac4",
    )
    second = broker.submit(
        symbol="MSTU", side="buy", notional=1174.13, ref_price=11.50,
        client_order_id="frac4",
    )
    assert len(client.submitted) == 1
    assert first.client_order_id == second.client_order_id


def test_not_fractionable_reraises_when_one_share_unaffordable():
    client = FakeNotFractionableClient()
    with pytest.raises(Exception, match="not fractionable"):
        _broker(client).submit(
            symbol="MSTU", side="buy", notional=50.0, ref_price=100.0,
            client_order_id="frac2",
        )
    assert client.submitted == []


def test_not_fractionable_reraises_without_ref_price():
    client = FakeNotFractionableClient()
    with pytest.raises(Exception, match="not fractionable"):
        _broker(client).submit(
            symbol="MSTU", side="buy", notional=1000.0, client_order_id="frac3"
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

    from alpaca.trading.enums import TimeInForce

    buy = _build_market_order_request(
        symbol="AAPL", side="buy", client_order_id="c1", notional=50.0
    )
    assert float(buy.notional) == 50.0 and buy.client_order_id == "c1"
    assert buy.time_in_force == TimeInForce.DAY
    sell = _build_market_order_request(
        symbol="AAPL", side="sell", client_order_id="c2", qty=3.0
    )
    assert float(sell.qty) == 3.0
    # Crypto symbols must use GTC (Alpaca rejects DAY for crypto)
    crypto_buy = _build_market_order_request(
        symbol="CRV/USD", side="buy", client_order_id="c3", notional=100.0
    )
    assert crypto_buy.time_in_force == TimeInForce.GTC
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


# ---- wash-trade rejection (40310000) ----

class FakeWashTradeClient(FakeClient):
    """Raises a wash-trade 403 on the first submit_order call, then succeeds."""

    CONFLICTING_ID = "ee1bfab0-d673-4a38-bf21-eae8fb81f49f"

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []
        self._rejected_once = False

    def submit_order(self, order_data):
        if not self._rejected_once:
            self._rejected_once = True
            raise Exception(
                '{"code":40310000,"existing_order_id":"'
                + self.CONFLICTING_ID
                + '","message":"potential wash trade detected. use complex orders",'
                '"reject_reason":"opposite side market/stop order exists"}'
            )
        return super().submit_order(order_data)

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancelled.append(order_id)


def test_wash_trade_rejection_cancels_conflict_and_retries():
    """40310000 should cancel the conflicting order and succeed on the retry."""
    client = FakeWashTradeClient()
    order = _broker(client).submit(
        symbol="ACHR", side="buy", notional=500.0, client_order_id="stop-achr"
    )
    assert FakeWashTradeClient.CONFLICTING_ID in client.cancelled
    assert len(client.submitted) == 1
    assert order.symbol == "ACHR"


def test_wash_trade_cancels_correct_conflicting_id():
    client = FakeWashTradeClient()
    _broker(client).submit(
        symbol="ACHR", side="buy", notional=500.0, client_order_id="stop-achr2"
    )
    assert client.cancelled == [FakeWashTradeClient.CONFLICTING_ID]


def test_non_wash_trade_error_still_propagates():
    """Unrelated 403s must not be silently swallowed."""
    class UnrelatedForbiddenClient(FakeClient):
        def submit_order(self, order_data):
            raise Exception('{"code":40310001,"message":"some other forbidden error"}')

    with pytest.raises(Exception, match="40310001"):
        _broker(UnrelatedForbiddenClient()).submit(
            symbol="AAPL", side="buy", notional=100.0, client_order_id="z"
        )


# ---- place_stop_order rejection handling ----

class StopRejectClient(FakeClient):
    """Rejects submissions with the given error messages, in order, then accepts."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__()
        self.errors = list(errors)

    def submit_order(self, order_data):
        if self.errors:
            raise Exception(self.errors.pop(0))
        self.submitted.append(order_data)
        return order_data


HTB_ERR = '{"code":42210000,"message":"only day orders are allowed for hard-to-borrow asset \\"GVH\\""}'
SHORT_ERR = '{"code":42210000,"message":"asset \\"NBIL\\" cannot be sold short"}'


def test_hard_to_borrow_stop_retries_as_day_with_fresh_id():
    client = StopRejectClient([HTB_ERR])
    order = _broker(client).place_stop_order(
        symbol="GVH", qty=5, stop_price=1.23, client_order_id="stop-gvh"
    )
    assert str(order.time_in_force).lower().endswith("day")
    assert order.client_order_id == "stop-gvh-day"


def test_short_sale_rejection_retries_with_fresh_id(monkeypatch):
    import trader.execution.broker as broker_mod
    monkeypatch.setattr(broker_mod.time, "sleep", lambda s: None)
    client = StopRejectClient([SHORT_ERR])
    order = _broker(client).place_stop_order(
        symbol="NBIL", qty=3, stop_price=2.5, client_order_id="stop-nbil"
    )
    assert order.client_order_id == "stop-nbil-r1"
    assert float(order.qty) == 3.0


def test_short_sale_rejection_exhausts_and_raises(monkeypatch):
    import trader.execution.broker as broker_mod
    monkeypatch.setattr(broker_mod.time, "sleep", lambda s: None)
    client = StopRejectClient([SHORT_ERR, SHORT_ERR, SHORT_ERR])
    with pytest.raises(Exception, match="cannot be sold short"):
        _broker(client).place_stop_order(
            symbol="NBIL", qty=3, stop_price=2.5, client_order_id="stop-nbil2"
        )


# ---- wait_for_fill ----

def test_wait_for_fill_returns_order_once_filled():
    client = FakeClient()
    client._by_coid["buy-1"] = SimpleNamespace(status="filled", filled_qty="5")
    order = _broker(client).wait_for_fill("buy-1", timeout=1.0, poll_interval=0.01)
    assert order is not None
    assert order.status == "filled"


def test_wait_for_fill_times_out_when_never_filled(monkeypatch):
    import trader.execution.broker as broker_mod
    monkeypatch.setattr(broker_mod.time, "sleep", lambda s: None)
    client = FakeClient()
    client._by_coid["buy-2"] = SimpleNamespace(status="pending_new", filled_qty="0")
    order = _broker(client).wait_for_fill("buy-2", timeout=1.0, poll_interval=0.01)
    assert order is None


def _fake_activity(activity_id: str, symbol: str = "AAPL", side: str = "buy") -> dict:
    return {
        "id": activity_id,
        "activity_type": "FILL",
        "symbol": symbol,
        "side": side,
        "qty": "1",
        "price": "100.0",
        "transaction_time": "2026-06-29T10:00:00Z",
        "order_id": f"order-{activity_id}",
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_get_account_activities_single_page(monkeypatch):
    import trader.execution.broker as broker_mod

    calls = []

    def _fake_get(url, headers, params, timeout):
        calls.append(params)
        return _FakeResponse([_fake_activity("1"), _fake_activity("2")])

    monkeypatch.setattr("requests.get", _fake_get)
    result = _broker(FakeClient()).get_account_activities("FILL")

    assert len(result) == 2
    assert len(calls) == 1
    assert calls[0]["page_size"] == 100  # never the old, over-the-cap 999


def test_get_account_activities_paginates_full_pages(monkeypatch):
    import trader.execution.broker as broker_mod

    pages = [
        [_fake_activity(str(i)) for i in range(100)],  # full page -> fetch next
        [_fake_activity("100")],                        # short page -> stop
    ]
    calls = []

    def _fake_get(url, headers, params, timeout):
        calls.append(params.get("page_token"))
        return _FakeResponse(pages.pop(0))

    monkeypatch.setattr("requests.get", _fake_get)
    result = _broker(FakeClient()).get_account_activities("FILL")

    assert len(result) == 101
    assert calls[0] is None
    assert calls[1] == "99"  # id of the last activity on the first page


def test_get_account_activities_returns_empty_on_error(monkeypatch):
    import trader.execution.broker as broker_mod

    def _fake_get(url, headers, params, timeout):
        raise RuntimeError("API down")

    monkeypatch.setattr("requests.get", _fake_get)
    assert _broker(FakeClient()).get_account_activities("FILL") == []


def test_wait_for_fill_tolerates_lookup_errors(monkeypatch):
    import trader.execution.broker as broker_mod
    monkeypatch.setattr(broker_mod.time, "sleep", lambda s: None)

    class _FlakyClient(FakeClient):
        def __init__(self):
            super().__init__()
            self._calls = 0

        def get_order_by_client_id(self, client_id):
            self._calls += 1
            if self._calls < 2:
                raise RuntimeError("transient network error")
            return SimpleNamespace(status="filled", filled_qty="1")

    order = _broker(_FlakyClient()).wait_for_fill("buy-3", timeout=1.0, poll_interval=0.01)
    assert order is not None
    assert order.status == "filled"


def test_place_stop_order_raises_for_fractional_qty():
    # Alpaca GTC stops must be whole shares; a fractional position must not be
    # silently left unprotected.
    with pytest.raises(ValueError, match="requires at least 1 whole share"):
        _broker(FakeClient()).place_stop_order(
            symbol="AAPL", qty=0.7, stop_price=90.0, client_order_id="stop-frac"
        )

