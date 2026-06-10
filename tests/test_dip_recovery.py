"""Tests for the DipRecovery strategy."""
from __future__ import annotations

import pandas as pd
import pytest

from trader.strategy.dip_recovery import DipRecovery, MIN_BARS


def _bars(closes: list[float], highs: list[float] | None = None) -> pd.DataFrame:
    """Synthetic daily bars. Highs default to close (flat candles)."""
    highs = highs if highs is not None else closes
    idx = pd.bdate_range("2024-01-02", periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": [min(c, h) for c, h in zip(closes, highs)],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=idx,
    )


def test_invalid_params_raise() -> None:
    with pytest.raises(ValueError):
        DipRecovery("TEST", dip_pct=0.0)
    with pytest.raises(ValueError):
        DipRecovery("TEST", dip_pct=1.5)
    with pytest.raises(ValueError):
        DipRecovery("TEST", expansion_pct=-0.1)


def test_insufficient_history_holds() -> None:
    bars = _bars([100.0] * (MIN_BARS - 1))
    signal = DipRecovery("TEST").generate(bars, bars.index[-1])
    assert signal.side == "hold"


def test_no_dip_holds() -> None:
    # Gentle climb, never 10% below the running high.
    closes = [100.0 + i * 0.1 for i in range(MIN_BARS + 10)]
    bars = _bars(closes)
    signal = DipRecovery("TEST").generate(bars, bars.index[-1])
    assert signal.side == "hold"


def test_dip_triggers_buy() -> None:
    closes = [100.0] * MIN_BARS + [89.0]  # 11% below the 100 ATH
    bars = _bars(closes)
    signal = DipRecovery("TEST").generate(bars, bars.index[-1])
    assert signal.side == "buy"
    assert signal.strength > 0.0


def test_deeper_dip_raises_strength() -> None:
    shallow = _bars([100.0] * MIN_BARS + [89.0])
    deep = _bars([100.0] * MIN_BARS + [75.0])
    strat = DipRecovery("TEST")
    s_shallow = strat.generate(shallow, shallow.index[-1])
    s_deep = strat.generate(deep, deep.index[-1])
    assert s_deep.strength > s_shallow.strength


def test_expansion_above_ath_sells() -> None:
    closes = [100.0] * MIN_BARS + [106.0]  # 6% above the prior 100 ATH
    bars = _bars(closes)
    signal = DipRecovery("TEST").generate(bars, bars.index[-1])
    assert signal.side == "sell"
    assert signal.strength == 1.0


def test_recovery_below_expansion_holds() -> None:
    # Recovered above the old high but not by the required 5%.
    closes = [100.0] * MIN_BARS + [89.0, 95.0, 103.0]
    bars = _bars(closes)
    signal = DipRecovery("TEST").generate(bars, bars.index[-1])
    assert signal.side == "hold"


def test_anchor_excludes_current_bar_high() -> None:
    # The exit bar prints a new high; the anchor must stay at the prior ATH so
    # the bar can still trigger the sell.
    closes = [100.0] * MIN_BARS + [106.0]
    highs = [100.0] * MIN_BARS + [108.0]
    bars = _bars(closes, highs)
    signal = DipRecovery("TEST").generate(bars, bars.index[-1])
    assert signal.side == "sell"


def test_no_lookahead() -> None:
    # asof pinned before the dip — strategy must not see the future crash.
    closes = [100.0] * MIN_BARS + [85.0]
    bars = _bars(closes)
    signal = DipRecovery("TEST").generate(bars, bars.index[MIN_BARS - 1])
    assert signal.side == "hold"
