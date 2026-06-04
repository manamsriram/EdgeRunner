"""Unit tests for go_live_gate threshold logic. No network, no Alpaca."""
from __future__ import annotations

import sys
import os

import pytest

# Add scripts/ to path so we can import the standalone script.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from go_live_gate import (  # noqa: E402
    BH_FLOOR_RATIO,
    MAX_DRAWDOWN,
    MIN_SHARPE,
    MIN_TRADE_COUNT,
    PASS_RATIO,
    _check_thresholds,
)


class _FakeMetrics:
    def __init__(self, sharpe=1.0, max_drawdown=-0.10, total_return=0.20):
        self.sharpe = sharpe
        self.max_drawdown = max_drawdown
        self.total_return = total_return


def _good() -> _FakeMetrics:
    return _FakeMetrics(sharpe=1.0, max_drawdown=-0.10, total_return=0.20)


def _bh(total_return=0.15) -> _FakeMetrics:
    return _FakeMetrics(total_return=total_return)


def test_all_thresholds_met_passes():
    passed, reason = _check_thresholds(_good(), _bh(), trade_count=10)
    assert passed
    assert reason == "all thresholds met"


def test_too_few_trades_fails():
    passed, reason = _check_thresholds(_good(), _bh(), trade_count=MIN_TRADE_COUNT - 1)
    assert not passed
    assert "too few trades" in reason


def test_low_sharpe_fails():
    m = _FakeMetrics(sharpe=MIN_SHARPE - 0.01)
    passed, reason = _check_thresholds(m, _bh(), trade_count=10)
    assert not passed
    assert "Sharpe" in reason


def test_deep_drawdown_fails():
    m = _FakeMetrics(max_drawdown=MAX_DRAWDOWN - 0.01)
    passed, reason = _check_thresholds(m, _bh(), trade_count=10)
    assert not passed
    assert "drawdown" in reason


def test_catastrophic_underperformance_fails():
    # Strategy return = 0%, B&H = 0.20 — floor is 0.20 * 0.8 = 0.16; 0% < 0.16 → fail
    m = _FakeMetrics(total_return=0.0)
    bh = _bh(total_return=0.20)
    passed, reason = _check_thresholds(m, bh, trade_count=10)
    assert not passed
    assert "B&H" in reason


def test_aggregate_pass_at_60_pct():
    """Verify PASS_RATIO constant matches the ≥60% design decision."""
    assert PASS_RATIO == 0.60


def test_aggregate_logic_5_of_8_passes():
    # Simulate the main() aggregation logic inline (no I/O).
    results = [True, True, True, True, True, False, False, False]
    passed_count = sum(results)
    total = len(results)
    assert passed_count / total >= PASS_RATIO  # 5/8 = 62.5% ≥ 60%


def test_aggregate_logic_4_of_8_fails():
    results = [True, True, True, True, False, False, False, False]
    passed_count = sum(results)
    total = len(results)
    assert passed_count / total < PASS_RATIO  # 4/8 = 50% < 60%
