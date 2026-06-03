"""Historical daily bars from Alpaca.

This is the data source for the strategy/backtest loop. We deliberately do NOT use the
yfinance path from tools/fetch_stock_info.py here: its `time.sleep(4)` rate-limit hack
would not survive a scheduler, and yfinance fundamentals are restated (not point-in-time).

Returns a tidy OHLCV DataFrame indexed by a tz-naive daily DatetimeIndex with columns
[open, high, low, close, volume] — the shape the Strategy/backtest code expects.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from trader.config import Config, load_config

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


def get_daily_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    config: Config | None = None,
) -> pd.DataFrame:
    """Fetch daily bars for a single symbol in [start, end].

    Imports of alpaca-py are local so the rest of the package (strategy, backtest,
    tests on synthetic data) does not require the SDK or network to be present.
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    config = config or load_config()
    config.require_alpaca()

    client = StockHistoricalDataClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
    )
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
    )
    bars = client.get_stock_bars(request)
    return _to_frame(bars.df, symbol)


def _to_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Normalise alpaca-py's MultiIndex (symbol, timestamp) frame into the standard
    single-symbol OHLCV frame used throughout the package."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)

    df = raw
    # alpaca-py returns a (symbol, timestamp) MultiIndex; drop to a timestamp index.
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")

    df = df.rename(columns=str.lower)[BAR_COLUMNS].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "date"
    return df.sort_index()
