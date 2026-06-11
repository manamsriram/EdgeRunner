"""Vol-targeted position sizing.

Scales the capital deployed at entry so each position contributes roughly
constant risk: scale = target_vol / realized_vol, clamped to [floor, 1.0].
Quiet markets get full size (never leverage up); loud markets get cut down to
the floor. Deterministic function of the bars, so the backtest engine replays
exactly what the live pipeline does.
"""
from __future__ import annotations

import math

import pandas as pd

from trader.strategy.regime import REALIZED_VOL_WINDOW, realized_vol

DEFAULT_TARGET_VOL = 0.20  # annualized
DEFAULT_FLOOR = 0.25       # never size below a quarter position


def vol_scale(
    bars: pd.DataFrame,
    target_vol: float = DEFAULT_TARGET_VOL,
    floor: float = DEFAULT_FLOOR,
) -> float:
    """Entry-size fraction in [floor, 1.0] for the current bars.

    Returns 1.0 (full size, today's behavior) whenever realized vol cannot be
    estimated: insufficient history, NaN, or zero vol. Never raises in the hot
    path.
    """
    if len(bars) < REALIZED_VOL_WINDOW + 1:
        return 1.0
    vol = float(realized_vol(bars["close"]).iloc[-1])
    if math.isnan(vol) or vol <= 0.0:
        return 1.0
    return float(min(1.0, max(floor, target_vol / vol)))
