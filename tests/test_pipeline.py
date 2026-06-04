"""Pipeline integration tests: routing logic, decision gate, fail-closed paths.

No network or Alpaca keys. Uses synthetic bars, a fake broker, and a file-based repo.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trader.config import Config, RiskLimits
from trader.execution.broker import AlpacaBroker
from trader.pipeline import run_pipeline
from trader.portfolio.repository import PROPOSAL_PENDING
from trader.portfolio.sqlite_repo import SQLiteRepository
from trader.risk.gate import AccountState, KillSwitch
from trader.strategy.base import Signal, Strategy


# ---- helpers ----

_SYMBOL = "AAPL"
_ALLOWLIST = ("AAPL",)
_ASOF = datetime(2023, 7, 1, 15, 0, tzinfo=timezone.utc)


def _config(tmp_path, autonomy: str = "manual", ks_name: str = "ks.flag") -> Config:
    return Config(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_paper=True,
        autonomy=autonomy,
        openai_api_key=None,
        anthropic_api_key=None,
        portfolio_db_path=str(tmp_path / "portfolio.db"),
        kill_switch_path=str(tmp_path / ks_name),
        risk=RiskLimits(
            max_position_pct=0.10,
            max_trades_per_day=5,
            daily_loss_limit_pct=0.03,
            allowlist=_ALLOWLIST,
        ),
    )


def _trending_bars() -> pd.DataFrame:
    n = 120
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    close = pd.Series(100.0 + np.arange(n) * 0.5, index=dates)
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 1_000_000,
    }, index=dates)


class _FixedStrategy(Strategy):
    """Returns a predetermined signal for testing."""

    def __init__(self, symbol: str, side: str, strength: float = 0.8) -> None:
        super().__init__(symbol)
        self._side = side
        self._strength = strength

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        return Signal(self.symbol, self._side, self._strength, f"fixed-{self._side}")


def _healthy_state(equity: float = 100_000.0) -> AccountState:
    return AccountState(
        equity=equity,
        positions={},
        open_order_symbols=frozenset(),
        trades_today=0,
        daily_pnl_pct=0.0,
        stale=False,
    )


def _stale_state() -> AccountState:
    return AccountState(
        equity=0.0,
        positions={},
        open_order_symbols=frozenset(),
        trades_today=0,
        daily_pnl_pct=None,
        stale=True,
    )


class FakeDuplicateError(Exception):
    def __init__(self) -> None:
        super().__init__("client_order_id already exists")


class FakeClient:
    def __init__(self, state: AccountState) -> None:
        self._state = state
        self.submitted: list = []
        self._by_coid: dict = {}

    def get_account(self):
        return SimpleNamespace(
            equity=str(self._state.equity),
            last_equity=str(self._state.equity),
        )

    def get_all_positions(self):
        return [
            SimpleNamespace(symbol=sym, qty=str(qty))
            for sym, qty in self._state.positions.items()
        ]

    def get_orders(self, filter):  # noqa: A002
        return []

    def get_clock(self):
        return SimpleNamespace(is_open=True)

    def submit_order(self, order_data: dict) -> Any:
        coid = order_data["client_order_id"]
        if coid in self._by_coid:
            raise FakeDuplicateError()
        order = SimpleNamespace(id="broker-id-1", **order_data)
        self._by_coid[coid] = order
        self.submitted.append(order)
        return order

    def get_order_by_client_id(self, client_id: str) -> Any:
        return self._by_coid[client_id]


def _fake_request_builder(*, symbol, side, client_order_id, notional=None, qty=None):
    return {"symbol": symbol, "side": side, "client_order_id": client_order_id,
            "notional": notional, "qty": qty}


def _fake_filter_builder(today):
    return "open", "closed"


def _broker_for(state: AccountState, config: Config) -> AlpacaBroker:
    return AlpacaBroker(
        config,
        client=FakeClient(state),
        request_builder=_fake_request_builder,
        order_filter_builder=_fake_filter_builder,
    )


_BARS = _trending_bars()


def _run(strategies, config, state=None):
    s = state or _healthy_state()
    b = _broker_for(s, config)
    r = SQLiteRepository(config.portfolio_db_path)

    import trader.pipeline as _pm
    original = _pm.get_daily_bars
    _pm.get_daily_bars = lambda symbol, start, end, config=None: _BARS
    try:
        return run_pipeline(config, strategies, b, r, asof=_ASOF), r, b
    finally:
        _pm.get_daily_bars = original


# ---- tests ----

def test_pipeline_manual_mode_queues_proposal(tmp_path):
    cfg = _config(tmp_path, autonomy="manual")
    results, repo, broker = _run([_FixedStrategy(_SYMBOL, "buy")], cfg)

    assert len(results) == 1
    result = results[0]
    assert result.outcome == "queued"
    assert result.proposal_id is not None
    assert result.order_id is None

    pending = repo.list_pending_proposals()
    assert len(pending) == 1
    assert pending[0]["symbol"] == _SYMBOL
    assert pending[0]["status"] == PROPOSAL_PENDING
    assert len(broker._client.submitted) == 0


def test_pipeline_auto_mode_executes(tmp_path):
    cfg = _config(tmp_path, autonomy="auto")
    results, repo, broker = _run([_FixedStrategy(_SYMBOL, "buy")], cfg)

    result = results[0]
    assert result.outcome == "executed"
    assert result.order_id is not None
    assert result.proposal_id is None

    orders = repo.get_orders()
    assert len(orders) == 1
    assert orders[0]["symbol"] == _SYMBOL
    assert len(broker._client.submitted) == 1


def test_pipeline_hold_signal_skips_gate(tmp_path):
    cfg = _config(tmp_path)
    results, repo, broker = _run([_FixedStrategy(_SYMBOL, "hold")], cfg)

    result = results[0]
    assert result.outcome == "hold"
    assert result.order_id is None
    assert result.proposal_id is None
    assert len(broker._client.submitted) == 0


def test_pipeline_blocked_by_empty_allowlist(tmp_path):
    cfg = Config(
        alpaca_api_key="k", alpaca_secret_key="s", alpaca_paper=True,
        autonomy="auto", openai_api_key=None, anthropic_api_key=None,
        portfolio_db_path=str(tmp_path / "portfolio.db"),
        kill_switch_path=str(tmp_path / "ks.flag"),
        risk=RiskLimits(allowlist=()),
    )
    results, repo, broker = _run([_FixedStrategy(_SYMBOL, "buy")], cfg)

    result = results[0]
    assert result.outcome == "blocked"
    assert result.proposal_id is None
    assert len(broker._client.submitted) == 0
    assert len(repo.list_pending_proposals()) == 0


def test_pipeline_kill_switch_blocks_all(tmp_path):
    cfg = _config(tmp_path, autonomy="auto")
    KillSwitch(cfg.kill_switch_path).engage("test")

    results, _, broker = _run([_FixedStrategy(_SYMBOL, "buy")], cfg)
    result = results[0]
    assert result.outcome == "blocked"
    assert len(broker._client.submitted) == 0


def test_pipeline_stale_reconciliation_blocks_all(tmp_path):
    """Broker reconcile failure → stale AccountState → gate rejects all symbols."""
    cfg = _config(tmp_path, autonomy="auto")

    class _BrokenClient(FakeClient):
        def get_account(self):
            raise RuntimeError("Alpaca API unreachable")

    b = AlpacaBroker(
        cfg,
        client=_BrokenClient(_healthy_state()),
        request_builder=_fake_request_builder,
        order_filter_builder=_fake_filter_builder,
    )
    r = SQLiteRepository(cfg.portfolio_db_path)

    import trader.pipeline as _pm
    original = _pm.get_daily_bars
    _pm.get_daily_bars = lambda symbol, start, end, config=None: _BARS
    try:
        results = run_pipeline(cfg, [_FixedStrategy(_SYMBOL, "buy")], b, r, asof=_ASOF)
    finally:
        _pm.get_daily_bars = original

    result = results[0]
    assert result.outcome == "blocked"
    assert "stale" in result.risk_decision.reason
    assert len(b._client.submitted) == 0


def test_pipeline_working_state_updated_between_symbols(tmp_path):
    """Regression: two buy signals in one tick with max_trades_per_day=1.

    The second symbol must be blocked after the first is executed, proving the
    pipeline updates working state (trades_today) between symbols rather than
    sharing the same pre-trade snapshot for both.
    """
    cfg = Config(
        alpaca_api_key="k",
        alpaca_secret_key="s",
        alpaca_paper=True,
        autonomy="auto",
        openai_api_key=None,
        anthropic_api_key=None,
        portfolio_db_path=str(tmp_path / "portfolio.db"),
        kill_switch_path=str(tmp_path / "ks.flag"),
        risk=RiskLimits(
            max_position_pct=0.10,
            max_trades_per_day=1,
            daily_loss_limit_pct=0.03,
            allowlist=("AAPL", "MSFT"),
        ),
    )
    results, repo, broker = _run(
        [_FixedStrategy("AAPL", "buy"), _FixedStrategy("MSFT", "buy")],
        cfg,
    )

    assert len(results) == 2
    outcomes = {r.symbol: r.outcome for r in results}
    # Exactly one executed, the other blocked by the updated trades_today counter.
    assert outcomes["AAPL"] == "executed"
    assert outcomes["MSFT"] == "blocked"
    assert len(broker._client.submitted) == 1
    assert len(repo.get_orders()) == 1
