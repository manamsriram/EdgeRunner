"""Tests for strategy edge cases and error handling."""
from __future__ import annotations

import pandas as pd
import pytest
from pandas import Timestamp

from trader.strategy.base import Signal
from trader.strategy.gap_pattern import GapPatternA
from trader.strategy.smash_day import SmashDayB
from trader.strategy.ma_crossover import MACrossover
from trader.strategy.momentum_rsi import MomentumRSI
from trader.strategy.crypto_trend import CryptoEMACrossover
from trader.strategy.crypto_mean_reversion import CryptoBollingerReversion


def test_strategies_handle_empty_data():
    """Test that all strategies handle empty data gracefully."""
    # Create empty DataFrame
    empty_bars = pd.DataFrame()

    # Test GapPatternA
    gap_strategy = GapPatternA("TEST")
    signal = gap_strategy.generate(empty_bars, Timestamp("2023-01-01"))
    assert signal.side == "hold"
    assert signal.reason == "no bar data"

    # Test SmashDayB
    smash_strategy = SmashDayB("TEST")
    signal = smash_strategy.generate(empty_bars, Timestamp("2023-01-01"))
    assert signal.side == "hold"
    assert signal.reason == "no bar data"

    # Test MACrossover
    ma_strategy = MACrossover("TEST")
    signal = ma_strategy.generate(empty_bars, Timestamp("2023-01-01"))
    assert signal.side == "hold"
    assert signal.reason == "no bar data"

    # Test MomentumRSI
    rsi_strategy = MomentumRSI("TEST")
    signal = rsi_strategy.generate(empty_bars, Timestamp("2023-01-01"))
    assert signal.side == "hold"
    assert signal.reason == "no bar data"

    # Test CryptoEMACrossover
    crypto_ema_strategy = CryptoEMACrossover("BTC/USD")
    signal = crypto_ema_strategy.generate(empty_bars, Timestamp("2023-01-01"))
    assert signal.side == "hold"
    assert signal.reason == "no bar data"

    # Test CryptoBollingerReversion
    crypto_bollinger_strategy = CryptoBollingerReversion("BTC/USD")
    signal = crypto_bollinger_strategy.generate(empty_bars, Timestamp("2023-01-01"))
    assert signal.side == "hold"
    assert signal.reason == "no bar data"


def test_strategies_handle_insufficient_bars():
    """Test that strategies handle insufficient bar data."""
    # Create DataFrame with minimal data
    dates = pd.date_range("2023-01-01", periods=2, freq="D")
    minimal_bars = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000, 1100],
        },
        index=dates,
    )

    # Test GapPatternA with insufficient data
    gap_strategy = GapPatternA("TEST", filter_n=20)
    signal = gap_strategy.generate(minimal_bars, Timestamp("2023-01-02"))
    assert signal.side == "hold"
    assert "insufficient" in signal.reason

    # Test SmashDayB with insufficient data
    smash_strategy = SmashDayB("TEST", trend_n=20)
    signal = smash_strategy.generate(minimal_bars, Timestamp("2023-01-02"))
    assert signal.side == "hold"
    assert "insufficient" in signal.reason

    # Test MACrossover with insufficient data
    ma_strategy = MACrossover("TEST", fast=5, slow=10)
    signal = ma_strategy.generate(minimal_bars, Timestamp("2023-01-02"))
    assert signal.side == "hold"
    assert "insufficient" in signal.reason

    # Test MomentumRSI with insufficient data
    rsi_strategy = MomentumRSI("TEST", lookback=5, rsi_window=14)
    signal = rsi_strategy.generate(minimal_bars, Timestamp("2023-01-02"))
    assert signal.side == "hold"
    assert "insufficient" in signal.reason

    # Test CryptoEMACrossover with insufficient data
    crypto_ema_strategy = CryptoEMACrossover("BTC/USD", fast=5, slow=10)
    signal = crypto_ema_strategy.generate(minimal_bars, Timestamp("2023-01-02"))
    assert signal.side == "hold"
    assert "insufficient" in signal.reason

    # Test CryptoBollingerReversion with insufficient data
    crypto_bollinger_strategy = CryptoBollingerReversion("BTC/USD", window=20)
    signal = crypto_bollinger_strategy.generate(minimal_bars, Timestamp("2023-01-02"))
    assert signal.side == "hold"
    assert "insufficient" in signal.reason


def test_state_reset_methods():
    """Test that state reset methods work correctly."""
    # Test GapPatternA reset
    gap_strategy = GapPatternA("TEST")
    gap_strategy._entry_bar_ts = Timestamp("2023-01-01")
    gap_strategy._gap_ref_level = 100.0
    gap_strategy.reset_state()
    assert gap_strategy._entry_bar_ts is None
    assert gap_strategy._gap_ref_level is None

    # Test SmashDayB reset
    smash_strategy = SmashDayB("TEST")
    smash_strategy._entry_bar_ts = Timestamp("2023-01-01")
    smash_strategy._entry_bar_low = 99.0
    smash_strategy.reset_state()
    assert smash_strategy._entry_bar_ts is None
    assert smash_strategy._entry_bar_low is None