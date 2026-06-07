"""Historical daily bars for crypto assets.

Returns the same tidy OHLCV DataFrame shape as alpaca_bars.get_daily_bars so
strategies and the backtest engine consume crypto and equities identically.

Two backends:
  alpaca — uses CryptoHistoricalDataClient (same credentials as the trading account).
            Symbol format: "BTC/USD", "ETH/USD".
  ccxt   — uses a public OHLCV endpoint on any CCXT-supported exchange (no auth needed
            for historical data). Symbol format follows the exchange convention, e.g.
            "BTC/USDT" on Binance.

IMPORTANT: uses config.require_alpaca_credentials() (NOT require_alpaca()) so that
this function works in both paper AND live mode.  The live-trading guard in
require_alpaca() is intentional for order submission, not for read-only data fetches.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from trader.config import Config, load_config

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


def get_crypto_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    config: Config | None = None,
    exchange: str | None = None,
) -> pd.DataFrame:
    """Fetch daily bars for a single crypto symbol in [start, end].

    `exchange` overrides config.crypto_exchange when provided.
    Returns a tz-naive daily DatetimeIndex DataFrame with columns [open, high, low, close, volume].
    """
    config = config or load_config()
    backend = (exchange or config.crypto_exchange).strip().lower()

    if backend == "alpaca":
        return _from_alpaca(symbol, start, end, config)
    return _from_ccxt(symbol, start, end, backend)


def _from_alpaca(
    symbol: str, start: datetime, end: datetime, config: Config
) -> pd.DataFrame:
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    config.require_alpaca_credentials()
    client = CryptoHistoricalDataClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
    )
    request = CryptoBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
    )
    bars = client.get_crypto_bars(request)
    return _to_frame(bars.df, symbol)


def _from_ccxt(symbol: str, start: datetime, end: datetime, exchange_name: str) -> pd.DataFrame:
    import ccxt

    exchange_cls = getattr(ccxt, exchange_name, None)
    if exchange_cls is None:
        raise ValueError(
            f"Unknown CCXT exchange: {exchange_name!r}. "
            f"Available: {ccxt.exchanges[:10]}..."
        )
    ex: ccxt.Exchange = exchange_cls()
    since_ms = int(start.timestamp() * 1000)
    limit = min(int((end - start).days) + 5, 1000)
    ohlcv = ex.fetch_ohlcv(symbol, "1d", since=since_ms, limit=limit)
    if not ohlcv:
        return pd.DataFrame(columns=BAR_COLUMNS)

    df = pd.DataFrame(ohlcv, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.tz_localize(None).normalize()
    df.index.name = "date"
    df = df[BAR_COLUMNS].copy()
    # Trim to requested range (CCXT may return a few extra rows).
    end_ts = pd.Timestamp(end).tz_localize(None).normalize()
    df = df.loc[df.index <= end_ts]
    return df.sort_index()


def _to_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Normalise alpaca-py's (symbol, timestamp) MultiIndex into the standard frame."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)

    df = raw
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")

    df = df.rename(columns=str.lower)[BAR_COLUMNS].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "date"
    return df.sort_index()
