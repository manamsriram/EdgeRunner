"""Historical daily bars from Alpaca.

This is the data source for the strategy/backtest loop. We deliberately do NOT use the
yfinance path from tools/fetch_stock_info.py here: its `time.sleep(4)` rate-limit hack
would not survive a scheduler, and yfinance fundamentals are restated (not point-in-time).

Returns a tidy OHLCV DataFrame indexed by a tz-naive daily DatetimeIndex with columns
[open, high, low, close, volume] — the shape the Strategy/backtest code expects.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from trader.config import Config, load_config

logger = logging.getLogger(__name__)

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
    from alpaca.data.enums import Adjustment, DataFeed

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
        feed=DataFeed.IEX,
        # Split/dividend-adjusted bars. The default (RAW) makes every split look
        # like a crash, which poisons drawdown- and lookback-based strategies.
        adjustment=Adjustment.ALL,
    )
    bars = client.get_stock_bars(request)
    return _to_frame(bars.df, symbol)


def get_daily_bars_batch(
    symbols: list[str],
    start: datetime,
    end: datetime,
    config: Config | None = None,
) -> dict[str, pd.DataFrame]:
    """Fetch daily bars for many symbols in a single API call.

    Returns {symbol: DataFrame}. Symbols with no data (halted, recently listed,
    delisted) are logged and omitted from the result rather than raising.
    Uses require_alpaca_credentials (not require_alpaca) — data fetch only, no orders.
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import Adjustment, DataFeed

    config = config or load_config()
    config.require_alpaca_credentials()

    if not symbols:
        return {}

    client = StockHistoricalDataClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
    )
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
        adjustment=Adjustment.ALL,
    )
    bars = client.get_stock_bars(request)

    result: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            result[sym] = _to_frame(bars.df, sym)
        except Exception:
            logger.warning("no bar data for %s — skipping", sym)
    return result


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
