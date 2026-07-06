"""Dip-recovery strategy — buy deep drawdowns from the all-time high, exit on
expansion above the prior high.

Port of the "NQ Drawdown Stack + ATH Expansion Exit" PineScript (daily timeframe).
The Pine original pyramids one contract every X% below the anchor ATH; the engine
and live pipeline are long/flat with a single position, so this port takes the
single-entry variant: enter once the drawdown reaches `dip_pct`, exit when price
recovers to `expansion_pct` above the pre-drawdown high. Conviction (strength)
scales with drawdown depth, standing in for the layer count of the original.

Stateless: the anchor high is recomputed from history each bar — while price is
underwater the prior ATH is frozen by construction (no newer bar exceeds it),
which exactly matches the Pine anchor that only ratchets while flat.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import Signal, Strategy
from trader.strategy.indicators import smooth_score
from trader.strategy.regime import Regime, classify_regime

MIN_BARS = 30


def _validate_params(dip_pct: float, expansion_pct: float) -> None:
    if not 0.0 < dip_pct < 1.0:
        raise ValueError("dip_pct must be in (0, 1)")
    if expansion_pct < 0.0:
        raise ValueError("expansion_pct must be >= 0")


class DipRecovery(Strategy):
    """Buy drawdowns of at least `dip_pct` from the prior all-time high; sell when
    close recovers to `expansion_pct` above that high.

    Parameters
    ----------
    symbol:         ticker symbol
    dip_pct:        drawdown from prior ATH that triggers a buy (default 0.10)
    expansion_pct:  recovery above prior ATH that triggers the exit (default 0.05)
    regime_params:  optional {regime: (dip_pct, expansion_pct)} table. When set,
                    the volatility regime is classified from the bars each call
                    and the matching params are used; regimes missing from the
                    table fall back to the base params. When None (default) the
                    strategy behaves exactly as the fixed-param version.
    smooth_window:  optional smoothing of the drawdown score before it's compared
                    to `dip_pct` — int for a rolling mean over that many bars,
                    float for an EWMA span. None (default) uses the raw single-bar
                    drawdown, matching prior behavior exactly. Damps single-bar
                    noise that would otherwise trigger a premature entry; the
                    expansion exit is left unsmoothed since exits should stay fast.
    """

    def __init__(
        self,
        symbol: str,
        dip_pct: float = 0.10,
        expansion_pct: float = 0.05,
        regime_params: dict[Regime, tuple[float, float]] | None = None,
        smooth_window: int | float | None = None,
    ) -> None:
        super().__init__(symbol)
        _validate_params(dip_pct, expansion_pct)
        if regime_params is not None:
            for regime, (r_dip, r_exp) in regime_params.items():
                if regime not in {"calm", "normal", "stressed"}:
                    raise ValueError(f"unknown regime: {regime!r}")
                _validate_params(r_dip, r_exp)
        if smooth_window is not None and smooth_window <= 0:
            raise ValueError("smooth_window must be > 0")
        self.dip_pct = dip_pct
        self.expansion_pct = expansion_pct
        self.regime_params = regime_params
        self.smooth_window = smooth_window

    def _effective_params(self, bars: pd.DataFrame) -> tuple[float, float, str]:
        """Resolve (dip_pct, expansion_pct, reason_tag) for the current bars."""
        if self.regime_params is None:
            return self.dip_pct, self.expansion_pct, ""
        regime = classify_regime(bars)
        dip_pct, expansion_pct = self.regime_params.get(
            regime, (self.dip_pct, self.expansion_pct)
        )
        return dip_pct, expansion_pct, f" [{regime}]"

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        if len(bars) < MIN_BARS:
            return Signal(self.symbol, "hold", 0.0, "insufficient history for ATH anchor")

        # Anchor on highs strictly before the current bar so a recovery bar that
        # prints a new high does not move its own exit level.
        prior_ath = float(bars["high"].iloc[:-1].max())
        curr_close = float(bars["close"].iloc[-1])
        if prior_ath <= 0:
            return Signal(self.symbol, "hold", 0.0, "no valid ATH anchor")

        drawdown = (prior_ath - curr_close) / prior_ath
        dip_pct, expansion_pct, regime_tag = self._effective_params(bars)

        if curr_close >= prior_ath * (1.0 + expansion_pct):
            return Signal(
                self.symbol, "sell", 1.0,
                f"close {curr_close:.2f} >= ATH {prior_ath:.2f} "
                f"+{expansion_pct:.0%}{regime_tag}",
            )

        buy_drawdown, smooth_tag = self._buy_drawdown(bars, drawdown)

        if buy_drawdown >= dip_pct:
            # Deeper dips earn more conviction; 2x the trigger depth maxes out.
            strength = float(min(buy_drawdown / (2.0 * dip_pct), 1.0))
            return Signal(
                self.symbol, "buy", strength,
                f"drawdown {buy_drawdown:.1%} >= {dip_pct:.0%} "
                f"from ATH {prior_ath:.2f}{regime_tag}{smooth_tag}",
            )

        return Signal(
            self.symbol, "hold", 0.0,
            f"drawdown {buy_drawdown:.1%} < {dip_pct:.0%}, no exit level hit{regime_tag}{smooth_tag}",
        )

    def _buy_drawdown(self, bars: pd.DataFrame, raw_drawdown: float) -> tuple[float, str]:
        """Drawdown score used for the *entry* threshold only. Raw unless
        `smooth_window` is set, in which case it's the smoothed drawdown series
        computed over history (each bar's drawdown vs. the ATH known before it)."""
        if self.smooth_window is None:
            return raw_drawdown, ""
        prior_ath_series = bars["high"].expanding().max().shift(1)
        drawdown_series = (prior_ath_series - bars["close"]) / prior_ath_series
        smoothed = smooth_score(drawdown_series, self.smooth_window)
        value = smoothed.iloc[-1]
        if pd.isna(value):
            return raw_drawdown, ""
        return float(value), " (smoothed)"
