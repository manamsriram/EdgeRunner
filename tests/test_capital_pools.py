from __future__ import annotations
import pytest
from trader.risk.gate import AccountState, OrderIntent, RiskDecision, RiskGate, KillSwitch
from trader.config import RiskLimits


def _state(**kw) -> AccountState:
    defaults = dict(
        equity=100_000.0, positions={}, open_order_symbols=frozenset(),
        trades_today=0, daily_pnl_pct=0.0, cash=100_000.0,
    )
    defaults.update(kw)
    return AccountState(**defaults)


def _limits(**kw) -> RiskLimits:
    defaults = dict(
        allowlist=None, max_position_pct=0.10,
        pdt_equity_threshold=25_000, pdt_day_trade_limit=3,
        intraday_pool_pct=0.40,
    )
    defaults.update(kw)
    return RiskLimits(**defaults)


def test_intraday_pool_pct_default():
    limits = RiskLimits()
    assert limits.intraday_pool_pct == 0.40


def test_max_trades_per_day_removed():
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(RiskLimits)}
    assert "max_trades_per_day" not in field_names


def test_account_state_has_intraday_deployed():
    state = _state()
    assert state.intraday_deployed == 0.0


def test_order_intent_default_pool_is_daily():
    intent = OrderIntent(symbol="AAPL", side="buy", notional=1000.0, ref_price=150.0)
    assert intent.pool == "daily"


def test_order_intent_pool_intraday():
    intent = OrderIntent(symbol="AAPL", side="buy", notional=1000.0, ref_price=150.0, pool="intraday")
    assert intent.pool == "intraday"


def test_gate_daily_pool_uses_daily_equity_fraction():
    """Daily cap = max_position_pct * equity * (1 - intraday_pool_pct) = 10% * 60k = $6,000."""
    limits = _limits()
    gate = RiskGate(limits)
    state = _state(equity=100_000.0, cash=100_000.0)
    intent = OrderIntent(symbol="AAPL", side="buy", notional=7_000.0, ref_price=150.0, pool="daily")
    decision = gate.evaluate(intent, state)
    assert decision.approved
    assert decision.approved_notional == pytest.approx(6_000.0, abs=1.0)


def test_gate_intraday_pool_uses_intraday_equity_fraction():
    """Intraday cap = max_position_pct * equity * intraday_pool_pct = 10% * 40k = $4,000."""
    limits = _limits()
    gate = RiskGate(limits)
    state = _state(equity=100_000.0, cash=100_000.0)
    intent = OrderIntent(symbol="AAPL", side="buy", notional=5_000.0, ref_price=150.0, pool="intraday")
    decision = gate.evaluate(intent, state)
    assert decision.approved
    assert decision.approved_notional == pytest.approx(4_000.0, abs=1.0)
