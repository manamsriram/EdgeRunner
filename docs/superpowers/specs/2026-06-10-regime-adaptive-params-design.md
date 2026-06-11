# Regime-Adaptive Strategy Parameters — Design

**Date:** 2026-06-10
**Status:** Approved

## Problem

Production strategies use fixed parameters. DipRecovery in particular buys at a
fixed 10% drawdown and exits at a fixed 5% expansion regardless of market
volatility — a dip depth that is meaningful in a calm market is noise in a
stressed one. We want parameters to adapt to live market conditions without
introducing live optimization (which overfits) or any adaptation logic that the
backtest engine cannot replay.

## Design Principle

**Every adaptation must be a deterministic function of price history.** The
backtest engine replays exactly what the live pipeline will do. No live
optimizer, no mutable config, no state outside the bars.

## Components

### 1. Regime detector — `trader/strategy/regime.py`

- Input: the same daily bars DataFrame strategies already receive.
- Computes 20-day realized volatility (annualized std of log returns), then its
  percentile rank against the trailing 252 trading days of the same measure.
- Buckets: `calm` (< 33rd percentile), `normal` (33rd–67th), `stressed`
  (> 67th percentile).
- Pure function: `classify_regime(bars) -> Regime`. Returns `normal` when
  history is insufficient (< 252 + 20 bars) so behavior degrades to the fixed
  baseline rather than failing.

### 2. Regime-adaptive DipRecovery

- `DipRecovery.__init__` gains an optional `regime_params` mapping:
  `{Regime: (dip_pct, expansion_pct)}`.
- When `regime_params` is None (default), behavior is byte-for-byte identical
  to today — fixed params. Backward compatible; existing tests unchanged.
- When provided, `_decide` classifies the regime from the bars it was given and
  looks up the effective `dip_pct` / `expansion_pct` for that bar.
- Param values per regime are **not hand-picked**: they come from the offline
  grid search (component 3).

### 3. Validation harness + gate

- Extend the backtest tooling (`scripts/backtest_combos.py` or a sibling
  script) with an adaptive-vs-fixed comparison:
  - **In-sample (years 1–2):** grid-search `dip_pct` / `expansion_pct` per
    regime bucket.
  - **Out-of-sample (years 3–4):** run the winning regime table against the
    fixed-param baseline.
- **Deployment gate:** the adaptive variant ships only if it beats the fixed
  baseline out-of-sample (total return, with drawdown sanity check). If it
  loses, the finding is documented and production keeps fixed params.

### 4. Vol-targeted position sizing — phase 2 (separate effort)

- Pipeline scales position notional by `target_vol / realized_vol`, capped at
  1.0, so stressed markets automatically get smaller positions.
- Out of scope for this spec; separately designed, backtested, and committed.

## Explicitly Rejected

- **Walk-forward live reoptimization:** re-fitting params on trailing windows
  in production chases noise and lags regimes. Rejected.
- **SuperTrend param adaptation:** ATR bands already widen with volatility and
  the ADX gate is already a regime filter. Marginal expected gain.
- **Regime-varying stop-loss:** DipRecovery is already stop-exempt and
  SuperTrend's trend-break exit fires before the 8% stop in most cases.

## Error Handling

- Insufficient history → `normal` regime → params equal current production
  values. No new failure modes in the live pipeline.
- `regime_params` validated at construction (same bounds checks as fixed
  params: `dip_pct` in (0, 1), `expansion_pct` >= 0).

## Testing

- Regime detector: unit tests for bucket boundaries, insufficient history,
  determinism on synthetic vol series.
- DipRecovery: tests that `regime_params=None` is identical to current
  behavior; tests that each regime selects its mapped params.
- Backtest: adaptive run must be reproducible (same seed-free deterministic
  output on repeated runs).

## Success Criteria

Adaptive DipRecovery beats fixed DipRecovery out-of-sample within the combo
backtest (ST+Dip sleeves, stop-loss model with DipRecovery exempt), or we keep
fixed params and record the negative result.
