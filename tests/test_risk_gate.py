"""Risk-gate tests: every guardrail, plus the fail-closed paths. No network, no broker.

These are the Phase 3 keystone checks — oversize sized down, breakers trip, allowlist
and pending-order and side-sanity enforced, and unknown/stale state rejected.
"""
from __future__ import annotations

import pytest

from trader.config import DEFAULT_ALLOWLIST, RiskLimits, _env_allowlist
from trader.risk.gate import AccountState, KillSwitch, OrderIntent, RiskDecision, RiskGate

LIMITS = RiskLimits(
    max_position_pct=0.10,
    max_trades_per_day=5,
    daily_loss_limit_pct=0.03,
    allowlist=("AAPL", "MSFT"),
)


def _state(**overrides) -> AccountState:
    base = dict(
        equity=100_000.0,
        positions={},
        open_order_symbols=frozenset(),
        trades_today=0,
        daily_pnl_pct=0.0,
        stale=False,
    )
    base.update(overrides)
    return AccountState(**base)


def _buy(symbol="AAPL", notional=1_000.0, ref_price=100.0) -> OrderIntent:
    return OrderIntent(symbol=symbol, side="buy", notional=notional, ref_price=ref_price)


@pytest.fixture
def gate() -> RiskGate:
    return RiskGate(LIMITS)


# ---- happy path ----

def test_clean_buy_approved(gate):
    decision = gate.evaluate(_buy(), _state())
    assert decision.approved
    assert decision.approved_notional == 1_000.0


# ---- max position size ----

def test_oversize_buy_sized_down_to_cap(gate):
    # cap = 10% of 100k = $10k, nothing held => a $25k buy is trimmed to $10k.
    decision = gate.evaluate(_buy(notional=25_000.0), _state())
    assert decision.approved
    assert decision.approved_notional == pytest.approx(10_000.0)


def test_buy_at_cap_with_existing_position_rejected_as_noop(gate):
    # Already holding 100 sh * $100 = $10k = the cap, so no headroom.
    decision = gate.evaluate(_buy(notional=5_000.0), _state(positions={"AAPL": 100.0}))
    assert not decision.approved
    assert "cap" in decision.reason.lower()


# ---- circuit breakers ----

def test_daily_loss_breaker_trips(gate):
    decision = gate.evaluate(_buy(), _state(daily_pnl_pct=-0.04))
    assert not decision.approved
    assert "daily loss" in decision.reason.lower()


def test_daily_pnl_unknown_is_failclosed(gate):
    decision = gate.evaluate(_buy(), _state(daily_pnl_pct=None))
    assert not decision.approved


def test_max_trades_per_day_rejected(gate):
    decision = gate.evaluate(_buy(), _state(trades_today=5))
    assert not decision.approved
    assert "max trades" in decision.reason.lower()


# ---- allowlist / pending / stale / kill switch ----

def test_off_allowlist_rejected(gate):
    decision = gate.evaluate(_buy(symbol="TSLA"), _state())
    assert not decision.approved
    assert "allowlist" in decision.reason.lower()


def test_pending_order_rejected(gate):
    decision = gate.evaluate(_buy(), _state(open_order_symbols=frozenset({"AAPL"})))
    assert not decision.approved
    assert "unfilled" in decision.reason.lower()


def test_stale_state_rejected_even_when_otherwise_clean(gate):
    decision = gate.evaluate(_buy(), _state(stale=True))
    assert not decision.approved
    assert "stale" in decision.reason.lower()


def test_kill_switch_rejects(gate, tmp_path):
    ks = KillSwitch(tmp_path / "kill.flag")
    ks.engage("test halt")
    assert ks.engaged()
    decision = gate.evaluate(_buy(), _state(), ks)
    assert not decision.approved
    assert "kill switch" in decision.reason.lower()
    ks.disengage()
    assert not ks.engaged()
    assert gate.evaluate(_buy(), _state(), ks).approved


# ---- side sanity (long/flat, no shorting) ----

def test_sell_without_position_rejected(gate):
    intent = OrderIntent(symbol="AAPL", side="sell", notional=1_000.0, ref_price=100.0)
    decision = gate.evaluate(intent, _state())
    assert not decision.approved
    assert "no aapl position" in decision.reason.lower()


def test_sell_with_position_approved(gate):
    intent = OrderIntent(symbol="AAPL", side="sell", notional=1_000.0, ref_price=100.0)
    decision = gate.evaluate(intent, _state(positions={"AAPL": 50.0}))
    assert decision.approved


def test_sell_notional_capped_to_held_value_no_short(gate):
    # Hold 5 sh * $100 = $500, but intent asks to sell $1000 — cap at held value.
    intent = OrderIntent(symbol="AAPL", side="sell", notional=1_000.0, ref_price=100.0)
    decision = gate.evaluate(intent, _state(positions={"AAPL": 5.0}))
    assert decision.approved
    assert decision.approved_notional == pytest.approx(500.0)


# ---- ordering: first failure wins ----

def test_first_failure_wins_kill_switch_before_allowlist(gate, tmp_path):
    ks = KillSwitch(tmp_path / "kill.flag")
    ks.engage()
    # Off-allowlist AND kill switch on — kill switch (check 0) must be the reason.
    decision = gate.evaluate(_buy(symbol="TSLA"), _state(), ks)
    assert decision.reason == "kill switch engaged"


# ---- intent validation ----

def test_intent_validation():
    with pytest.raises(ValueError):
        OrderIntent("AAPL", "short", 100.0, 10.0)
    with pytest.raises(ValueError):
        OrderIntent("AAPL", "buy", -1.0, 10.0)
    with pytest.raises(ValueError):
        OrderIntent("AAPL", "buy", 100.0, 0.0)


# ---- allowlist env fallback (never silently empty) ----

def test_allowlist_env_blank_falls_back_to_basket(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWLIST", "   ,  ")
    assert _env_allowlist("RISK_ALLOWLIST", DEFAULT_ALLOWLIST) == DEFAULT_ALLOWLIST


def test_allowlist_env_parsed_upper_stripped(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWLIST", " aapl, msft ,nvda")
    assert _env_allowlist("RISK_ALLOWLIST", DEFAULT_ALLOWLIST) == ("AAPL", "MSFT", "NVDA")
