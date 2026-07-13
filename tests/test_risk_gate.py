"""Risk-gate tests: every guardrail, plus the fail-closed paths. No network, no broker.

These are the Phase 3 keystone checks — oversize sized down, breakers trip, allowlist
and pending-order and side-sanity enforced, and unknown/stale state rejected.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.config import DEFAULT_ALLOWLIST, RiskLimits, _env_allowlist
from trader.risk.gate import (
    AccountState, KillSwitch, OrderIntent, RiskDecision, RiskGate,
    is_crypto_symbol, is_option_symbol,
)


def test_is_option_symbol_matches_occ_and_rejects_equity_crypto():
    assert is_option_symbol("AAPL260116P00150000")
    assert is_option_symbol("SPY251219C00450000")
    assert not is_option_symbol("AAPL")
    assert not is_option_symbol("BTC/USD")
    assert not is_crypto_symbol("AAPL260116P00150000")  # OCC must not read as crypto

LIMITS = RiskLimits(
    max_position_pct=0.10,
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
    # cap = 10% * 100k * (1 - 0.40 daily pool) = $6,000; a $25k buy is trimmed to $6k.
    decision = gate.evaluate(_buy(notional=25_000.0), _state())
    assert decision.approved
    assert decision.approved_notional == pytest.approx(6_000.0)


def test_buy_at_cap_with_existing_position_rejected_as_noop(gate):
    # Already holding 100 sh * $100 = $10k = the cap, so no headroom.
    decision = gate.evaluate(_buy(notional=5_000.0), _state(positions={"AAPL": 100.0}))
    assert not decision.approved
    assert "cap" in decision.reason.lower()


# ---- circuit breakers ----

HALT_LIMITS = RiskLimits(
    max_position_pct=0.10,
    daily_loss_limit_pct=0.03,
    allowlist=("AAPL", "MSFT"),
    daily_loss_halt_enabled=True,
)


def _halt_gate() -> RiskGate:
    return RiskGate(HALT_LIMITS)


def test_daily_loss_halt_blocks_new_buy_when_enabled():
    decision = _halt_gate().evaluate(_buy(), _state(daily_pnl_pct=-0.04))
    assert not decision.approved
    assert "daily loss" in decision.reason.lower()


def test_daily_loss_halt_never_blocks_sells():
    intent = OrderIntent(symbol="AAPL", side="sell", notional=1_000.0, ref_price=100.0)
    decision = _halt_gate().evaluate(intent, _state(positions={"AAPL": 10.0}, daily_pnl_pct=-0.04))
    assert decision.approved


def test_daily_loss_halt_skips_when_pnl_unknown():
    # crypto/CCXT has no last_equity → daily_pnl_pct is None; must never fail-closed.
    decision = _halt_gate().evaluate(_buy(), _state(daily_pnl_pct=None))
    assert decision.approved


def test_daily_loss_halt_disabled_by_default_ignores_daily_loss(gate):
    # LIMITS (default fixture) has daily_loss_halt_enabled=False — no behavior change.
    decision = gate.evaluate(_buy(), _state(daily_pnl_pct=-0.04))
    assert decision.approved


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


# ---- PDT guard ----

PDT_LIMITS = RiskLimits(
    max_position_pct=0.10,
    daily_loss_limit_pct=0.03,
    allowlist=("AAPL", "MSFT"),
    pdt_equity_threshold=25_000.0,
    pdt_day_trade_limit=3,
)


def _pdt_gate() -> RiskGate:
    return RiskGate(PDT_LIMITS)


def test_pdt_blocks_buy_when_round_trips_reached():
    # 6 fills = 3 round-trips on a $20k account → buy rejected
    state = _state(equity=20_000.0, trades_today=6)
    decision = _pdt_gate().evaluate(_buy(), state)
    assert not decision.approved
    assert "PDT guard" in decision.reason


def test_pdt_allows_buy_when_round_trips_below_limit():
    # 4 fills = 2 round-trips → still below limit
    state = _state(equity=20_000.0, trades_today=4)
    decision = _pdt_gate().evaluate(_buy(), state)
    assert decision.approved


def test_pdt_does_not_apply_above_equity_threshold():
    # $30k account: PDT guard inactive regardless of trade count
    state = _state(equity=30_000.0, trades_today=6)
    decision = _pdt_gate().evaluate(_buy(), state)
    assert decision.approved


def test_pdt_never_blocks_sells():
    # Sells must always be allowed — closing a position cannot be blocked by PDT
    state = _state(equity=20_000.0, trades_today=6, positions={"AAPL": 10.0})
    sell = OrderIntent(symbol="AAPL", side="sell", notional=1_000.0, ref_price=100.0)
    decision = _pdt_gate().evaluate(sell, state)
    assert decision.approved


# ---- symbol cooldown ----

COOLDOWN_LIMITS = RiskLimits(
    max_position_pct=0.10,
    allowlist=("AAPL", "MSFT"),
    symbol_cooldown_enabled=True,
    symbol_cooldown_seconds=3600,
)


def _cooldown_gate() -> RiskGate:
    return RiskGate(COOLDOWN_LIMITS)


def test_cooldown_blocks_buy_within_window():
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    state = _state(last_losing_exit_at={"AAPL": now - timedelta(minutes=30)})
    decision = _cooldown_gate().evaluate(_buy(), state, now=now)
    assert not decision.approved
    assert "cooldown" in decision.reason.lower()


def test_cooldown_allows_buy_once_elapsed():
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    state = _state(last_losing_exit_at={"AAPL": now - timedelta(hours=2)})
    decision = _cooldown_gate().evaluate(_buy(), state, now=now)
    assert decision.approved


def test_cooldown_disabled_by_default_ignores_recent_loss():
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    state = _state(last_losing_exit_at={"AAPL": now - timedelta(minutes=1)})
    decision = gate_default().evaluate(_buy(), state, now=now)
    assert decision.approved


def gate_default() -> RiskGate:
    return RiskGate(LIMITS)


def test_cooldown_never_blocks_sells():
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    state = _state(
        positions={"AAPL": 10.0},
        last_losing_exit_at={"AAPL": now - timedelta(minutes=1)},
    )
    sell = OrderIntent(symbol="AAPL", side="sell", notional=1_000.0, ref_price=100.0)
    decision = _cooldown_gate().evaluate(sell, state, now=now)
    assert decision.approved


def test_kill_switch_wins_over_cooldown(tmp_path):
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    ks = KillSwitch(tmp_path / "kill.flag")
    ks.engage()
    state = _state(last_losing_exit_at={"AAPL": now - timedelta(minutes=1)})
    decision = _cooldown_gate().evaluate(_buy(), state, ks, now=now)
    assert decision.reason == "kill switch engaged"
