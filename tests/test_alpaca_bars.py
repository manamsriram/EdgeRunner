"""Data-layer guards: live-quote failures propagate (so callers can alert on stale
eval) and the daily-bars cache is safe under concurrent scheduler threads."""
from __future__ import annotations

import threading
from datetime import datetime

import pandas as pd
import pytest

from trader.data import alpaca_bars


def _cfg():
    return type("C", (), {
        "alpaca_api_key": "k", "alpaca_secret_key": "s",
        "require_alpaca_credentials": lambda self: None,
    })()


def _raw_bars(symbol: str) -> pd.DataFrame:
    idx = pd.MultiIndex.from_tuples(
        [(symbol, pd.Timestamp("2026-07-01"))], names=["symbol", "timestamp"]
    )
    return pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
        index=idx,
    )


def test_live_quote_failure_propagates(monkeypatch):
    """get_live_prices_batch must not swallow a fetch failure — the pipeline relies on
    the exception to alert that stop-loss is evaluating against a stale close."""
    class FailingClient:
        def __init__(self, **kw): ...
        def get_stock_latest_quote(self, req):
            raise RuntimeError("alpaca down")

    monkeypatch.setattr(
        "alpaca.data.historical.StockHistoricalDataClient", FailingClient
    )
    with pytest.raises(RuntimeError):
        alpaca_bars.get_live_prices_batch(["AAPL"], _cfg())


def test_bars_cache_concurrent_fetch_populates_once(monkeypatch):
    """Two threads hammering the same symbol → one network fetch, no KeyError/torn read."""
    fetch_count = {"n": 0}

    class CountingClient:
        def __init__(self, **kw): ...
        def get_stock_bars(self, req):
            fetch_count["n"] += 1
            # slow enough that both threads would race without the lock
            threading.Event().wait(0.05)
            return type("R", (), {"df": _raw_bars("AAPL")})()

    monkeypatch.setattr(
        "alpaca.data.historical.StockHistoricalDataClient", CountingClient
    )
    # reset module cache
    alpaca_bars._bars_cache = {}
    alpaca_bars._bars_cache_date = None

    start = datetime(2026, 6, 1)
    end = datetime(2026, 7, 2)
    results: list[dict] = []
    errors: list[Exception] = []

    def worker():
        try:
            results.append(alpaca_bars.get_daily_bars_batch(["AAPL"], start, end, _cfg()))
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert fetch_count["n"] == 1          # lock prevented a double fetch
    assert all("AAPL" in r for r in results)
