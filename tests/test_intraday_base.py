from __future__ import annotations
import pandas as pd
from trader.strategy.base import IntradayStrategy, Signal


class _Stub(IntradayStrategy):
    def _decide(self, bars, asof):
        return Signal(self.symbol, "hold", 0.0, "stub")


def test_intraday_strategy_flags():
    s = _Stub("AAPL")
    assert s.pool == "intraday"
    assert s.eod_exit is True
    assert s.skip_fundamental_gate is True
    assert s.skip_overlay is True
    assert s.bar_timeframe == "5min"
    assert s.lookback_minutes == 390


def test_intraday_strategy_override_timeframe():
    class _OnMin(IntradayStrategy):
        bar_timeframe = "1min"
        def _decide(self, bars, asof):
            return Signal(self.symbol, "hold", 0.0, "stub")
    s = _OnMin("AAPL")
    assert s.bar_timeframe == "1min"


def test_intraday_bars_index_is_minute_level():
    """_to_intraday_frame must NOT normalize the index to dates."""
    import numpy as np
    idx = pd.date_range("2024-01-15 09:30", periods=30, freq="1min")
    raw = pd.DataFrame({
        "open": np.ones(30), "high": np.ones(30),
        "low": np.ones(30), "close": np.ones(30), "volume": np.ones(30),
    }, index=idx)
    from trader.data.alpaca_bars import _to_intraday_frame
    result = _to_intraday_frame(raw, "AAPL")
    assert result.index.dtype != "datetime64[ns]" or result.index[0].hour != 0, \
        "index must keep intraday timestamps, not normalize to midnight"
    assert result.index[0].hour == 9
    assert result.index[0].minute == 30
