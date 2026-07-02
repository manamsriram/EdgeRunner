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
        cash=equity,
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
            cash=str(self._state.cash),
        )

    def get_all_positions(self):
        return [
            SimpleNamespace(
                symbol=sym, qty=str(qty),
                avg_entry_price=str(self._state.avg_entry_prices.get(sym, 0.0)),
            )
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
        # Market orders fill near-instantly in paper trading; simulate that so
        # AlpacaBroker.wait_for_fill doesn't block real tests on a real sleep.
        order = SimpleNamespace(id="broker-id-1", status="filled", filled_qty="0", **order_data)
        self._by_coid[coid] = order
        self.submitted.append(order)
        return order

    def get_order_by_client_id(self, client_id: str) -> Any:
        return self._by_coid[client_id]

    def get_portfolio_history(self) -> Any:
        return None


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
    original_single = _pm.get_daily_bars
    original_batch = _pm.get_daily_bars_batch
    _pm.get_daily_bars = lambda symbol, start, end, config=None: _BARS
    _pm.get_daily_bars_batch = lambda symbols, start, end, config=None: {s: _BARS for s in symbols}
    try:
        return run_pipeline(config, strategies, b, r, asof=_ASOF), r, b
    finally:
        _pm.get_daily_bars = original_single
        _pm.get_daily_bars_batch = original_batch


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
    original_single = _pm.get_daily_bars
    original_batch = _pm.get_daily_bars_batch
    _pm.get_daily_bars = lambda symbol, start, end, config=None: _BARS
    _pm.get_daily_bars_batch = lambda symbols, start, end, config=None: {s: _BARS for s in symbols}
    try:
        results = run_pipeline(cfg, [_FixedStrategy(_SYMBOL, "buy")], b, r, asof=_ASOF)
    finally:
        _pm.get_daily_bars = original_single
        _pm.get_daily_bars_batch = original_batch

    result = results[0]
    assert result.outcome == "blocked"
    assert "stale" in result.risk_decision.reason
    assert len(b._client.submitted) == 0


def test_auto_mode_fires_fill_alert(tmp_path, monkeypatch):
    """Auto-mode execute path must call send_alert with symbol and 'FILL'."""
    alerts_fired: list[str] = []

    def _fake_alert(message: str, webhook_url, **kwargs) -> None:
        alerts_fired.append(message)

    monkeypatch.setattr("trader.pipeline.send_alert", _fake_alert)

    cfg = _config(tmp_path, autonomy="auto")
    # Give the config a fake webhook so alerts are not no-op'd.
    from dataclasses import replace as _replace
    cfg = _replace(cfg, slack_webhook_url="https://hooks.example.com/test")

    _run([_FixedStrategy(_SYMBOL, "buy")], cfg)

    fill_alerts = [m for m in alerts_fired if "FILL" in m and _SYMBOL in m]
    assert fill_alerts, f"Expected a FILL alert for {_SYMBOL}; got: {alerts_fired}"


def test_pipeline_working_state_updated_between_symbols(tmp_path):
    """Both buy signals execute now that max_trades_per_day cap is disabled."""
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
    assert outcomes["AAPL"] == "executed"
    assert outcomes["MSFT"] == "executed"
    assert len(broker._client.submitted) == 2
    assert len(repo.get_orders()) == 2


# ---- ownership pruning for retired strategies ----

class _OtherStrategy(_FixedStrategy):
    """Distinct class name so ownership comparisons see a different strategy."""


def _held_state() -> AccountState:
    return AccountState(
        equity=100_000.0,
        positions={_SYMBOL: 10.0},
        open_order_symbols=frozenset(),
        trades_today=0,
        daily_pnl_pct=0.0,
        stale=False,
        cash=100_000.0,
        avg_entry_prices={_SYMBOL: 100.0},
    )


def test_sell_allowed_when_owner_strategy_retired(tmp_path):
    """A position owned by a strategy that is no longer in the active stack must
    not be orphaned: ownership is pruned so remaining strategies can exit it."""
    cfg = _config(tmp_path, autonomy="manual")
    SQLiteRepository(cfg.portfolio_db_path).set_position_owner(_SYMBOL, "DonchianBreakout")

    results, _, _ = _run([_FixedStrategy(_SYMBOL, "sell")], cfg, state=_held_state())

    assert len(results) == 1
    assert results[0].outcome == "queued"  # sell proposal, not ownership-blocked


def test_auto_mode_sell_records_trade_outcome(tmp_path):
    """A real (auto-mode) sell fill writes exactly one trade_outcomes row with the
    correct entry basis, exit price, and exit_reason classification."""
    cfg = _config(tmp_path, autonomy="auto")

    results, repo, _ = _run([_FixedStrategy(_SYMBOL, "sell")], cfg, state=_held_state())

    assert results[0].outcome == "executed"
    outcomes = repo.get_recent_outcomes(symbol=_SYMBOL)
    assert len(outcomes) == 1
    assert outcomes[0]["entry_price"] == pytest.approx(100.0)
    assert outcomes[0]["exit_reason"] == "signal-exit"


def test_manual_mode_queued_sell_records_no_trade_outcome(tmp_path):
    """A manual-mode 'queued' proposal is not a real fill — recording an outcome
    here would feed fabricated P&L into cooldown/trade-memory. Locks in the v1 scope
    cut: manual-mode trades don't produce trade_outcomes rows until the ProposalRow/
    approve() gap is closed in a follow-up."""
    cfg = _config(tmp_path, autonomy="manual")

    results, repo, _ = _run([_FixedStrategy(_SYMBOL, "sell")], cfg, state=_held_state())

    assert results[0].outcome == "queued"
    assert repo.get_recent_outcomes(symbol=_SYMBOL) == []


def test_sell_still_blocked_when_owner_strategy_active(tmp_path):
    """Ownership must keep blocking cross-strategy sells while the owner runs."""
    cfg = _config(tmp_path, autonomy="manual")
    SQLiteRepository(cfg.portfolio_db_path).set_position_owner(_SYMBOL, "_OtherStrategy")

    results, _, _ = _run(
        [_FixedStrategy(_SYMBOL, "sell"), _OtherStrategy(_SYMBOL, "hold")],
        cfg,
        state=_held_state(),
    )

    sell_result = next(r for r in results if r.signal and r.signal.side == "sell")
    assert sell_result.outcome == "blocked"
    assert "ownership conflict" in sell_result.risk_decision.reason


# ---- bandit weighting ----

class _StratA(_FixedStrategy):
    """Distinct class name for bandit tests — AAPL, low raw strength."""


class _StratB(_FixedStrategy):
    """Distinct class name for bandit tests — MSFT, high raw strength."""


def _bandit_config(tmp_path, *, shadow: bool = False, live: bool = False) -> Config:
    from dataclasses import replace as _replace
    return Config(
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
            daily_loss_limit_pct=0.03,
            allowlist=("AAPL", "MSFT"),
            bandit_weighting_shadow=shadow,
            bandit_weighting_live=live,
        ),
    )


def test_bandit_live_mode_reorders_buys_by_effective_strength(tmp_path):
    """Live bandit: lower-raw-strength strategy with higher weight executes first.

    _StratA AAPL: raw 0.5, weight 1.5 → effective 0.75
    _StratB MSFT: raw 0.8, weight 0.4 → effective 0.32
    Without bandit MSFT (0.8) goes first; live mode should put AAPL first.
    """
    cfg = _bandit_config(tmp_path, live=True)
    SQLiteRepository(cfg.portfolio_db_path).save_bandit_weight("_StratA", "normal", 1.5, cycle_index=1)
    SQLiteRepository(cfg.portfolio_db_path).save_bandit_weight("_StratB", "normal", 0.4, cycle_index=1)

    _, _, broker = _run(
        [_StratA("AAPL", "buy", strength=0.5), _StratB("MSFT", "buy", strength=0.8)],
        cfg,
    )

    assert broker._client.submitted[0].symbol == "AAPL"
    assert broker._client.submitted[1].symbol == "MSFT"


def test_bandit_shadow_mode_does_not_change_ranking(tmp_path):
    """Shadow mode: weights computed/logged but raw strength still determines order."""
    cfg = _bandit_config(tmp_path, shadow=True)
    SQLiteRepository(cfg.portfolio_db_path).save_bandit_weight("_StratA", "normal", 1.5, cycle_index=1)
    SQLiteRepository(cfg.portfolio_db_path).save_bandit_weight("_StratB", "normal", 0.4, cycle_index=1)

    _, _, broker = _run(
        [_StratA("AAPL", "buy", strength=0.5), _StratB("MSFT", "buy", strength=0.8)],
        cfg,
    )

    assert broker._client.submitted[0].symbol == "MSFT"
    assert broker._client.submitted[1].symbol == "AAPL"


def test_bandit_off_uses_raw_strength_ordering(tmp_path):
    """Baseline: both flags off → raw strength orders buys regardless of stored weights."""
    cfg = _bandit_config(tmp_path)
    SQLiteRepository(cfg.portfolio_db_path).save_bandit_weight("_StratA", "normal", 1.5, cycle_index=1)
    SQLiteRepository(cfg.portfolio_db_path).save_bandit_weight("_StratB", "normal", 0.4, cycle_index=1)

    _, _, broker = _run(
        [_StratA("AAPL", "buy", strength=0.5), _StratB("MSFT", "buy", strength=0.8)],
        cfg,
    )

    assert broker._client.submitted[0].symbol == "MSFT"
    assert broker._client.submitted[1].symbol == "AAPL"
