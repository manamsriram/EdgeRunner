from __future__ import annotations

import numpy as np


def compute_ic(strengths: list[float], returns: list[float]) -> float | None:
    """Pearson corr(strength, return). Returns None when < 5 pairs or if inputs differ in length."""
    if len(strengths) < 5 or len(strengths) != len(returns):
        return None
    return float(np.corrcoef(strengths, returns)[0, 1])


def compute_icir(ic_series: list[float], window: int = 20) -> float | None:
    """Rolling ICIR over last `window` values. Returns None when < 3 or std == 0."""
    recent = ic_series[-window:]
    if len(recent) < 3:
        return None
    std = float(np.std(recent))
    if std == 0:
        return None
    return float(np.mean(recent) / std)


def ic_weight_nudge(icir: float | None, scale: float = 0.05) -> float:
    """Small additive delta in [-scale, +scale]. Zero when ICIR unavailable."""
    if icir is None:
        return 0.0
    return float(np.clip(icir * scale, -scale, scale))
