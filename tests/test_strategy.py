"""Strategy contract tests: valid Signals, and no lookahead in the indicators."""
from __future__ import annotations

import pytest

from trader.strategy.base import Signal
from trader.strategy.ma_crossover import MACrossover
from trader.strategy.momentum_rsi import MomentumRSI


@pytest.mark.parametrize("make", [
    lambda: MACrossover("X", fast=20, slow=50),
    lambda: MomentumRSI("X", lookback=20),
])
def test_strategy_emits_valid_signal(make, trending_bars):
    strat = make()
    asof = trending_bars.index[-1]
    sig = strat.generate(trending_bars, asof)
    assert isinstance(sig, Signal)
    assert sig.side in {"buy", "sell", "hold"}
    assert 0.0 <= sig.strength <= 1.0


@pytest.mark.parametrize("make", [
    lambda: MACrossover("X", fast=20, slow=50),
    lambda: MomentumRSI("X", lookback=20),
])
def test_signal_identical_full_vs_truncated(make, trending_bars):
    """The decision at `asof` must not change whether the strategy is handed the full
    series or only the bars up to `asof` — i.e. nothing after `asof` is used."""
    strat = make()
    asof = trending_bars.index[80]
    full_sig = strat.generate(trending_bars, asof)
    truncated_sig = strat.generate(trending_bars.loc[:asof], asof)
    assert full_sig == truncated_sig


def test_ma_crossover_buys_on_uptrend(trending_bars):
    # A steadily rising series => fast SMA above slow SMA => buy.
    sig = MACrossover("X", fast=20, slow=50).generate(trending_bars, trending_bars.index[-1])
    assert sig.side == "buy"


def test_signal_validation():
    with pytest.raises(ValueError):
        Signal("X", "longe", 0.5, "bad side")
    with pytest.raises(ValueError):
        Signal("X", "buy", 1.5, "bad strength")
