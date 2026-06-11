"""Tests for the dynamic crypto universe screener ranking logic."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trader.universe.crypto_screener import rank_candidates

NOW = datetime(2026, 6, 11, 6, 0, 0, tzinfo=timezone.utc)


def _bar(hours_ago: float, close: float, volume: float) -> SimpleNamespace:
    return SimpleNamespace(
        timestamp=NOW - timedelta(hours=hours_ago),
        close=close,
        volume=volume,
    )


def test_ranks_by_dollar_volume_descending():
    bars = {
        "BTC/USD": _bar(1, close=60_000.0, volume=10.0),      # $600k
        "DOGE/USD": _bar(1, close=0.10, volume=20_000_000.0),  # $2M
        "ETH/USD": _bar(1, close=2_000.0, volume=100.0),       # $200k
    }
    assert rank_candidates(bars, top_n=3, now=NOW) == [
        "DOGE/USD", "BTC/USD", "ETH/USD",
    ]


def test_unit_volume_does_not_dominate_ranking():
    # Cheap coin with huge unit volume but tiny dollar volume must rank below BTC.
    bars = {
        "SHIB/USD": _bar(1, close=0.00001, volume=1_000_000.0),  # $10
        "BTC/USD": _bar(1, close=60_000.0, volume=1.0),          # $60k
    }
    assert rank_candidates(bars, top_n=2, now=NOW) == ["BTC/USD", "SHIB/USD"]


def test_excludes_stale_bars():
    # Delisted pairs surface as old bars (e.g. MATIC's latest bar from 2023).
    bars = {
        "MATIC/USD": _bar(24 * 365, close=0.68, volume=1e9),
        "BTC/USD": _bar(1, close=60_000.0, volume=1.0),
    }
    assert rank_candidates(bars, top_n=10, now=NOW) == ["BTC/USD"]


def test_respects_top_n():
    bars = {f"C{i}/USD": _bar(1, close=1.0, volume=float(i)) for i in range(5)}
    result = rank_candidates(bars, top_n=2, now=NOW)
    assert result == ["C4/USD", "C3/USD"]


def test_empty_input_returns_empty():
    assert rank_candidates({}, top_n=10, now=NOW) == []


def test_zero_dollar_volume_excluded():
    bars = {
        "YFI/USD": _bar(1, close=1_881.0, volume=0.0),
        "BTC/USD": _bar(1, close=60_000.0, volume=1.0),
    }
    assert rank_candidates(bars, top_n=10, now=NOW) == ["BTC/USD"]
