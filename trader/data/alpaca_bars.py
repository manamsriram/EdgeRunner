"""Historical daily bars from Alpaca.

This is the data source for the strategy/backtest loop. We deliberately do NOT use the
yfinance path from tools/fetch_stock_info.py here: its `time.sleep(4)` rate-limit hack
would not survive a scheduler, and yfinance fundamentals are restated (not point-in-time).

Returns a tidy OHLCV DataFrame indexed by a tz-naive daily DatetimeIndex with columns
[open, high, low, close, volume] — the shape the Strategy/backtest code expects.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime

import pandas as pd

from trader.config import Config, load_config

logger = logging.getLogger(__name__)

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]

# Daily bars don't change intraday — cache per symbol, invalidate at day boundary.
# Read/written from equity, crypto, and trade-stream threads.
# ponytail: one global lock for the whole cache module; shard by symbol only if
# lock contention shows up in profiling.
_bars_cache_lock = threading.Lock()
_bars_cache: dict[str, pd.DataFrame] = {}
_bars_cache_date: date | None = None


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

    Results are cached by symbol for the calendar day — daily bars don't change
    intraday, so repeated 60s scheduler ticks reuse the same DataFrames.
    """
    global _bars_cache, _bars_cache_date

    if not symbols:
        return {}

    # Hold the lock across the whole read-fetch-write so two threads can't both
    # fetch the same missing symbols or observe a torn/half-cleared cache at the
    # day boundary. The network fetch inside is the slow part, but daily bars are
    # fetched once per calendar day per symbol, so contention is negligible.
    with _bars_cache_lock:
        today = end.date()
        if _bars_cache_date != today:
            _bars_cache = {}
            _bars_cache_date = today

        missing = [s for s in symbols if s not in _bars_cache]
        if missing:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            from alpaca.data.enums import Adjustment, DataFeed

            config = config or load_config()
            config.require_alpaca_credentials()

            client = StockHistoricalDataClient(
                api_key=config.alpaca_api_key,
                secret_key=config.alpaca_secret_key,
            )
            request = StockBarsRequest(
                symbol_or_symbols=missing,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=DataFeed.IEX,
                adjustment=Adjustment.ALL,
            )
            bars = client.get_stock_bars(request)
            # Strip today's partial bar so only completed trading days are cached.
            # Intraday partial bars cause stale signals on every subsequent 60s tick.
            today_ts = pd.Timestamp.today().normalize()
            for sym in missing:
                try:
                    df = _to_frame(bars.df, sym)
                    _bars_cache[sym] = df[df.index < today_ts]
                except Exception:
                    logger.warning("no bar data for %s — skipping", sym)
            logger.debug("bars cache miss: fetched %d symbols, cache now %d", len(missing), len(_bars_cache))

        return {s: _bars_cache[s] for s in symbols if s in _bars_cache}


def get_live_prices_batch(
    symbols: list[str],
    config: Config | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Fetch latest bid/ask midpoint and spread for equity symbols. No caching.

    Returns (mid_prices, spread_pcts). Both dicts keyed by symbol; symbols with no
    quote are omitted — callers fall back to bars[-1].close / spread_pct=0 for those.
    spread_pct = (ask - bid) / mid, useful for transaction cost filtering in the gate.
    """
    if not symbols:
        return {}, {}

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest

    config = config or load_config()
    config.require_alpaca_credentials()

    client = StockHistoricalDataClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
    )
    # Let failures propagate: both pipeline callers wrap this and fall back to
    # yesterday's close, and the equity caller alerts once/day on the stale eval.
    # Swallowing here made that staleness silent.
    quotes = client.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=symbols)
    )

    mids: dict[str, float] = {}
    spread_pcts: dict[str, float] = {}
    for sym, q in quotes.items():
        bid = float(getattr(q, "bid_price", 0) or 0)
        ask = float(getattr(q, "ask_price", 0) or 0)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            mids[sym] = mid
            spread_pcts[sym] = (ask - bid) / mid if mid > 0 else 0.0
        elif ask > 0:
            mids[sym] = ask
        elif bid > 0:
            mids[sym] = bid
    return mids, spread_pcts


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


def _to_intraday_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Normalise alpaca-py MultiIndex frame for intraday bars.

    Unlike _to_frame, does NOT normalize to daily dates — minute timestamps preserved.
    """
    if raw is None or raw.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = raw
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    df = df.rename(columns=str.lower)[BAR_COLUMNS].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "timestamp"
    return df.sort_index()


def get_intraday_bars_batch(
    symbols: list[str],
    timeframe: str,
    lookback_minutes: int = 390,
    config: "Config | None" = None,
) -> dict[str, pd.DataFrame]:
    """Fetch intraday bars for many symbols in one API call. No cache.

    timeframe: "1min" → TimeFrame.Minute; "5min" → TimeFrame(5, TimeFrameUnit.Minute)
    lookback_minutes: bars fetched from (now - lookback_minutes); 390 = full trading day.
    Returns {symbol: DataFrame} with minute-level DatetimeIndex.
    """
    from datetime import timezone as _tz
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    from datetime import timedelta

    if not symbols:
        return {}

    config = config or load_config()
    config.require_alpaca_credentials()

    if timeframe == "1min":
        tf = TimeFrame.Minute
    elif timeframe == "5min":
        tf = TimeFrame(5, TimeFrameUnit.Minute)
    else:
        raise ValueError(f"unsupported intraday timeframe: {timeframe!r}")

    now = datetime.now(_tz.utc)
    start = now - timedelta(minutes=lookback_minutes + 10)

    client = StockHistoricalDataClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
    )
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=tf,
        start=start,
        end=now,
        feed=DataFeed.IEX,
    )
    try:
        bars = client.get_stock_bars(request)
    except Exception:
        logger.warning("intraday bar fetch failed for %d symbols", len(symbols))
        return {}

    raw_df = bars.df
    result: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            result[sym] = _to_intraday_frame(raw_df, sym)
        except Exception:
            logger.warning("no intraday bar data for %s — skipping", sym)
    return result
