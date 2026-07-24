"""Tests for DonchianBreakout strategy."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.strategy.base import Signal
from trader.strategy.donchian_breakout import DonchianBreakout


def _make_bars(closes) -> pd.DataFrame:
    n = len(closes)
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    c = pd.Series(closes, index=dates, dtype=float)
    return pd.DataFrame({
        "open": c.shift(1).fillna(c.iloc[0]),
        "high": c + 0.5,
        "low": c - 0.5,
        "close": c,
        "volume": 1_000_000,
    }, index=dates)


def _breakout_bars(n: int = 70) -> pd.DataFrame:
    """Uptrend bars where the last close breaks above the prior 20-bar high."""
    # First n-1 bars: slowly rising to 110
    base = list(np.linspace(100.0, 110.0, n - 1))
    # Flat top for 20 bars so prior high is well-defined
    base[-20:] = [110.0] * 20
    # Last bar: decisive breakout above prior 20-bar high
    closes = base + [113.0]
    return _make_bars(closes)


# ---- contract ---------------------------------------------------------------

def test_returns_valid_signal():
    bars = _breakout_bars()
    sig = DonchianBreakout("X").generate(bars, bars.index[-1])
    assert isinstance(sig, Signal)
    assert sig.side in {"buy", "sell", "hold"}
    assert 0.0 <= sig.strength <= 1.0


def test_insufficient_history_returns_hold():
    bars = _make_bars([100.0] * 10)
    sig = DonchianBreakout("X").generate(bars, bars.index[-1])
    assert sig.side == "hold"


# ---- entry ------------------------------------------------------------------

def test_buy_on_donchian_breakout():
    bars = _breakout_bars(n=70)
    sig = DonchianBreakout("X", channel_n=20, trend_n=20).generate(bars, bars.index[-1])
    assert sig.side == "buy"
    assert sig.strength > 0.0


def test_no_buy_without_trend_filter():
    """Breakout in a downtrend should be suppressed."""
    n = 70
    # Downtrend then brief spike — close always below start
    closes = list(np.linspace(130.0, 110.0, n - 1)) + [114.0]
    bars = _make_bars(closes)
    sig = DonchianBreakout("X", channel_n=20, trend_n=20).generate(bars, bars.index[-1])
    # Trend filter: close must be above close[-(1+trend_n)] — here close is lower
    assert sig.side in {"hold", "sell"}


def test_no_buy_when_no_breakout():
    n = 60
    closes = list(np.linspace(100.0, 105.0, n))  # slow grind, no clean breakout
    bars = _make_bars(closes)
    sig = DonchianBreakout("X", channel_n=20).generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_no_buy_on_continuous_uptrend_without_flat_top():
    """Guard rejects entry if prior bar was already above the channel high (continuation, not fresh breakout)."""
    n = 70
    closes = list(np.linspace(100.0, 110.0, n - 1)) + [113.0]
    bars = _make_bars(closes)
    sig = DonchianBreakout("X", channel_n=20, trend_n=20).generate(bars, bars.index[-1])
    assert sig.side == "hold"


# ---- exit -------------------------------------------------------------------

def test_time_exit_after_hold_limit():
    bars = _breakout_bars(n=70)
    strat = DonchianBreakout("X", channel_n=20, trend_n=20, time_exit=5)
    # Trigger entry
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "buy"

    # Extend bars 5 more days (flat — no quick exit triggered)
    last_close = float(bars["close"].iloc[-1])
    new_dates = pd.date_range(bars.index[-1] + pd.tseries.offsets.BDay(1), periods=5, freq="B")
    extra = pd.DataFrame({
        "open": last_close,
        "high": last_close + 0.5,
        "low": last_close - 0.3,
        "close": last_close,
        "volume": 1_000_000,
    }, index=new_dates)
    extended = pd.concat([bars, extra])
    sig2 = strat.generate(extended, extended.index[-1])
    assert sig2.side == "sell"


def test_quick_exit_when_close_drops_below_entry_low():
    bars = _breakout_bars(n=70)
    strat = DonchianBreakout("X", channel_n=20, trend_n=20)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "buy"
    entry_low = strat._entry_bar_low

    # Next bar: close drops well below entry bar's low
    drop_close = entry_low - 1.0
    new_date = bars.index[-1] + pd.tseries.offsets.BDay(1)
    extra = pd.DataFrame({
        "open": drop_close,
        "high": drop_close + 0.2,
        "low": drop_close - 0.2,
        "close": drop_close,
        "volume": 1_000_000,
    }, index=pd.DatetimeIndex([new_date]))
    extended = pd.concat([bars, extra])
    sig2 = strat.generate(extended, extended.index[-1])
    assert sig2.side == "sell"


# ---- warm_up (cold-start reconstruction) -------------------------------------

def test_warm_up_reconstructs_entry_older_than_time_exit():
    """A restart can happen well after time_exit bars have already elapsed since
    entry (frequent deploys). warm_up must still find that older breakout and
    hand it straight to an immediate time-exit, not lose track of it forever."""
    bars = _breakout_bars(n=70)
    last_close = float(bars["close"].iloc[-1])
    # 15 more flat bars after the breakout — real entry is now 15 bars back,
    # beyond the old `time_exit`-bounded scan window (default time_exit=10).
    new_dates = pd.date_range(bars.index[-1] + pd.tseries.offsets.BDay(1), periods=15, freq="B")
    extra = pd.DataFrame({
        "open": last_close, "high": last_close + 0.5,
        "low": last_close - 0.3, "close": last_close, "volume": 1_000_000,
    }, index=new_dates)
    extended = pd.concat([bars, extra])

    strat = DonchianBreakout("X", channel_n=20, trend_n=20, time_exit=10)
    strat.warm_up(extended, has_position=True)
    assert strat._entry_bar_ts == bars.index[-1]

    sig = strat.generate(extended, extended.index[-1])
    assert sig.side == "sell"


def test_warm_up_force_exits_position_predating_fetched_window():
    """If the position was opened before the fetched bar window even starts (no
    breakout visible anywhere in history), warm_up must not leave it untracked
    forever — it should anchor old enough to force an exit on the next tick."""
    n = 60
    closes = list(np.linspace(100.0, 105.0, n))  # slow grind, no breakout anywhere
    bars = _make_bars(closes)

    strat = DonchianBreakout("X", channel_n=20, trend_n=20, time_exit=10)
    strat.warm_up(bars, has_position=True)
    assert strat._entry_bar_ts is not None

    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "sell"


# ---- empty data -------------------------------------------------------------

def test_empty_bars_returns_hold():
    bars = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    bars.index = pd.DatetimeIndex([])
    sig = DonchianBreakout("X").generate(bars, pd.Timestamp("2023-01-02"))
    assert sig.side == "hold"
