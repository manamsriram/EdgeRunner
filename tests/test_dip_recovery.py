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
    # 11% below the 100 ATH, then an uptick bar to confirm the fall has paused.
    closes = [100.0] * MIN_BARS + [89.0, 90.0]
    bars = _bars(closes)
    signal = DipRecovery("TEST").generate(bars, bars.index[-1])
    assert signal.side == "buy"
    assert signal.strength > 0.0


def test_dip_without_uptick_holds() -> None:
    # Deep enough drawdown, but still falling on the current bar — must wait
    # for confirmation instead of catching the falling knife.
    closes = [100.0] * MIN_BARS + [92.0, 89.0]
    bars = _bars(closes)
    signal = DipRecovery("TEST").generate(bars, bars.index[-1])
    assert signal.side == "hold"


def test_deeper_dip_raises_strength() -> None:
    shallow = _bars([100.0] * MIN_BARS + [89.0, 90.0])
    deep = _bars([100.0] * MIN_BARS + [75.0, 76.0])
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


# ---- Regime-adaptive parameters ----

REGIME_TABLE = {
    "calm": (0.08, 0.05),
    "normal": (0.10, 0.05),
    "stressed": (0.15, 0.05),
}


def _closes_from_returns(returns: list[float]) -> list[float]:
    closes = [100.0]
    for r in returns:
        closes.append(closes[-1] * (1.0 + r))
    return closes[1:]


def _quiet_then_crash_bars() -> pd.DataFrame:
    """Quiet year, then a high-vol decline: stressed regime, ~13.8% drawdown."""
    history = []
    for _ in range(150):  # reciprocal pairs keep price pinned near 100
        history += [0.002, 1.0 / 1.002 - 1.0]
    crash = []
    for _ in range(20):  # down-first so the crash never prints a new high
        crash += [-0.0332, 0.0268]
    return _bars(_closes_from_returns(history + crash))


def _wild_then_quiet_decline_bars() -> pd.DataFrame:
    """Volatile year, then a near-zero-vol drift down: calm regime, ~8.9% drawdown."""
    history = []
    for _ in range(150):
        history += [0.02, 1.0 / 1.02 - 1.0]
    # Final bar upticks slightly to confirm the fall has paused.
    decline = [-0.00184] * 39 + [0.001]
    return _bars(_closes_from_returns(history + decline))


def test_regime_fixture_classification() -> None:
    from trader.strategy.regime import classify_regime

    assert classify_regime(_quiet_then_crash_bars()) == "stressed"
    assert classify_regime(_wild_then_quiet_decline_bars()) == "calm"


def test_invalid_regime_params_raise() -> None:
    with pytest.raises(ValueError):
        DipRecovery("TEST", regime_params={"stressed": (1.5, 0.05)})
    with pytest.raises(ValueError):
        DipRecovery("TEST", regime_params={"calm": (0.08, -0.1)})


def test_stressed_regime_uses_deeper_dip_threshold() -> None:
    bars = _quiet_then_crash_bars()
    fixed = DipRecovery("TEST").generate(bars, bars.index[-1])
    adaptive = DipRecovery("TEST", regime_params=REGIME_TABLE).generate(
        bars, bars.index[-1]
    )
    # ~13.8% drawdown: deep enough for the fixed 10% trigger, not for the
    # stressed-regime 15% trigger.
    assert fixed.side == "buy"
    assert adaptive.side == "hold"


def test_calm_regime_uses_shallower_dip_threshold() -> None:
    bars = _wild_then_quiet_decline_bars()
    fixed = DipRecovery("TEST").generate(bars, bars.index[-1])
    adaptive = DipRecovery("TEST", regime_params=REGIME_TABLE).generate(
        bars, bars.index[-1]
    )
    # ~8.9% drawdown: shallow of the fixed 10% trigger, deep enough for the
    # calm-regime 8% trigger.
    assert fixed.side == "hold"
    assert adaptive.side == "buy"


def test_missing_regime_falls_back_to_base_params() -> None:
    bars = _quiet_then_crash_bars()  # stressed regime
    adaptive = DipRecovery("TEST", regime_params={"calm": (0.08, 0.05)}).generate(
        bars, bars.index[-1]
    )
    fixed = DipRecovery("TEST").generate(bars, bars.index[-1])
    assert adaptive.side == fixed.side == "buy"


def test_uniform_regime_table_matches_fixed_params() -> None:
    bars = _quiet_then_crash_bars()
    uniform = {r: (0.10, 0.05) for r in ("calm", "normal", "stressed")}
    fixed = DipRecovery("TEST").generate(bars, bars.index[-1])
    adaptive = DipRecovery("TEST", regime_params=uniform).generate(bars, bars.index[-1])
    assert adaptive.side == fixed.side
    assert adaptive.strength == fixed.strength


# ---- Drawdown smoothing ----

def test_invalid_smooth_window_raises() -> None:
    with pytest.raises(ValueError):
        DipRecovery("TEST", smooth_window=0)
    with pytest.raises(ValueError):
        DipRecovery("TEST", smooth_window=-5)


def test_smooth_window_none_matches_unsmoothed_default() -> None:
    closes = [100.0] * MIN_BARS + [89.0]
    bars = _bars(closes)
    default = DipRecovery("TEST").generate(bars, bars.index[-1])
    explicit_none = DipRecovery("TEST", smooth_window=None).generate(bars, bars.index[-1])
    assert default.side == explicit_none.side
    assert default.strength == explicit_none.strength


def test_smoothing_damps_single_bar_noise_below_trigger() -> None:
    # Two noisy bars dip ~10.5% intraday-close but the surrounding bars sit at
    # the ATH — raw drawdown crosses the 10% trigger on the uptick bar that
    # confirms the dip, a 5-bar rolling mean should not (it's mostly zeros).
    closes = [100.0] * MIN_BARS + [89.0, 89.5] + [100.0] * 3
    bars = _bars(closes)
    raw = DipRecovery("TEST").generate(bars, bars.index[-4])  # the uptick bar
    smoothed = DipRecovery("TEST", smooth_window=5).generate(bars, bars.index[-4])
    assert raw.side == "buy"
    assert smoothed.side == "hold"


def test_sustained_dip_still_triggers_when_smoothed() -> None:
    # A real, sustained drawdown should still cross the threshold once smoothed
    # over the same window it dipped for. Final bar upticks to confirm the dip.
    closes = [100.0] * MIN_BARS + [89.0] * 4 + [89.5]
    bars = _bars(closes)
    smoothed = DipRecovery("TEST", smooth_window=5).generate(bars, bars.index[-1])
    assert smoothed.side == "buy"


def test_ewma_smooth_window_accepted() -> None:
    closes = [100.0] * MIN_BARS + [89.0] * 7 + [89.5]
    bars = _bars(closes)
    signal = DipRecovery("TEST", smooth_window=4.0).generate(bars, bars.index[-1])
    assert signal.side == "buy"


def test_smoothing_does_not_affect_exit() -> None:
    # Expansion exit is a hard breakout check — smoothing must not delay it.
    closes = [100.0] * MIN_BARS + [106.0]
    bars = _bars(closes)
    signal = DipRecovery("TEST", smooth_window=10).generate(bars, bars.index[-1])
    assert signal.side == "sell"
    assert signal.strength == 1.0
