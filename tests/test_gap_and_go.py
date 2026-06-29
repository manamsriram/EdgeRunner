from __future__ import annotations
import pandas as pd
import pytest
from trader.strategy.base import IntradayStrategy
from trader.strategy.gap_and_go import GapAndGo


def _make_bars(opens, closes, volumes=None, start="2024-01-15 09:30") -> pd.DataFrame:
    n = len(closes)
    ts = pd.date_range(start, periods=n, freq="1min")
    v = pd.Series(volumes or [2_000_000] * n, index=ts, dtype=float)
    c = pd.Series(closes, index=ts, dtype=float)
    o = pd.Series(opens, index=ts, dtype=float)
    return pd.DataFrame({
        "open": o,
        "high": c + 0.5,
        "low": c - 0.5,
        "close": c,
        "volume": v,
    }, index=ts)


def test_is_intraday_strategy():
    assert isinstance(GapAndGo("AAPL"), IntradayStrategy)


def test_bar_timeframe_is_1min():
    assert GapAndGo("AAPL").bar_timeframe == "1min"


def test_hold_before_entry_window():
    """Bars 0-4 (9:30-9:34): no entry yet."""
    strat = GapAndGo("AAPL")
    strat.prev_close = 100.0
    opens = [103.0] * 5  # gap up 3%
    closes = [103.5] * 5
    bars = _make_bars(opens, closes, volumes=[3_000_000] * 5)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_buy_on_valid_gap_in_entry_window():
    """Bar 5 (9:35): gap>2%, volume>1.5x avg, price > prev_close → buy."""
    strat = GapAndGo("AAPL")
    strat.prev_close = 100.0
    avg_vol = 1_000_000
    # bars 0-4: normal volume; bar 5: high volume gap
    volumes = [avg_vol] * 5 + [avg_vol * 2]
    opens = [103.0] * 6
    closes = [103.5] * 6
    bars = _make_bars(opens, closes, volumes=volumes)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "buy"


def test_no_entry_after_window_close():
    """Bars 0-9 pass by; bar 10 is after window — no entry allowed."""
    strat = GapAndGo("AAPL")
    strat.prev_close = 100.0
    volumes = [2_000_000] * 10
    opens = [103.0] * 10
    closes = [103.5] * 10
    bars = _make_bars(opens, closes, volumes=volumes)
    sig = strat.generate(bars, bars.index[-1])
    # window closed — hold or no entry
    assert sig.side in {"hold", "sell"}


def test_hold_when_gap_insufficient():
    """Gap < 2% → no entry."""
    strat = GapAndGo("AAPL")
    strat.prev_close = 100.0
    volumes = [2_000_000] * 6
    opens = [101.0] * 6  # only 1% gap
    closes = [101.5] * 6
    bars = _make_bars(opens, closes, volumes=volumes)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_sell_when_momentum_fades():
    """After entry, close < entry_bar_open → sell."""
    from datetime import date
    strat = GapAndGo("AAPL")
    strat.prev_close = 100.0
    strat._entered = True
    strat._entry_bar_open = 103.0
    strat._last_session_date = date(2024, 1, 15)
    volumes = [2_000_000] * 6
    opens = [103.0] * 6
    closes = [103.5] * 5 + [102.5]  # last bar: close < entry_bar_open
    bars = _make_bars(opens, closes, volumes=volumes)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "sell"
