"""Feature preprocessing shared by dataset building and live inference.

`build_feature_vector` (features.py) emits the RAW snapshot that gets persisted to
decision_features. This module turns that raw snapshot into the vector the model
actually consumes. It lives in one place on purpose: a preprocessing step duplicated
between the training script and the inference path is how a model silently degrades
in production — the two copies drift and nothing fails loudly.

Two transformations:

1. Drop the `regime_*` one-hots. `classify_regime` has emitted only 'normal' for the
   entire logged history (all 18k+ rows), so they carry zero information and would
   only contribute noise coefficients. Revisit if regime classification is ever fixed.

2. Split the trade-history sentinel. `days_since_last_trade` is -1.0 when the symbol
   has no closed trades, which is ~85% of rows — standardizing that column would crush
   the real day-counts against a sentinel-dominated mean/std. `last_trade_pnl_pct` and
   `win_rate_last_3` are 0.0 on those same rows, where 0.0 is also a legitimate value,
   so the model otherwise cannot tell "no history" from "last trade was flat". One
   explicit `has_trade_history` flag disambiguates all three.
"""
from __future__ import annotations

_DROPPED_KEYS = ("regime_calm", "regime_normal", "regime_stressed")

_SENTINEL_KEY = "days_since_last_trade"


def derive(raw: dict[str, float]) -> dict[str, float]:
    """Map a raw feature snapshot to the model's input vector.

    Pure and deterministic. Order of the returned dict is not meaningful — callers
    must index by the artifact's `feature_names`, never by iteration order.
    """
    out = {k: float(v) for k, v in raw.items() if k not in _DROPPED_KEYS}

    days = float(raw.get(_SENTINEL_KEY, -1.0))
    out["has_trade_history"] = 1.0 if days >= 0.0 else 0.0
    out[_SENTINEL_KEY] = max(days, 0.0)

    return out
