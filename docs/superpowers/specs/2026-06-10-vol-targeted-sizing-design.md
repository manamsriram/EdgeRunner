# Vol-Targeted Position Sizing — Design

**Date:** 2026-06-10
**Status:** Validated — negative result, flat sizing retained (see Outcome)

## Problem

Buy notional in the live pipeline is a flat fraction of free cash
(`_notional_for`: `cap_pct * free_cash`) regardless of how volatile the symbol
currently is. A position opened during a high-volatility period carries far
more risk per dollar than one opened in a quiet period. Vol targeting scales
the entry so each position contributes roughly constant risk.

## Design Principle (carried from phase 1)

Every adaptation is a deterministic function of price history. The backtest
engine replays exactly what the live pipeline will do. No live optimizer.

## Lesson from the stop-loss work

A uniform risk overlay can destroy a strategy whose edge *is* taking
high-vol risk. DipRecovery buys deep dips — by construction high-vol moments —
and its edge died under the uniform 8% stop until it was exempted. Vol-sizing
has the same conflict potential, so validation tests per-strategy application:
ST-only versus ST+DipRecovery.

## Components

### 1. Sizing function — `trader/risk/vol_sizing.py`

```
vol_scale(bars, target_vol, floor) -> float in [floor, 1.0]
```

- Realized vol: reuse `trader.strategy.regime.realized_vol` (20-day annualized).
- Scale = `clamp(target_vol / realized_vol, floor, 1.0)`.
- Returns 1.0 (full size) on insufficient history, NaN, or zero vol — degrades
  to current behavior, never errors.
- `floor` (default 0.25) keeps entries economically meaningful; cap at 1.0
  means never leveraging up in quiet markets, only de-risking in loud ones.

### 2. Backtest engine — entry-fraction support

`run_backtest` gains optional `entry_fraction: Callable[[pd.DataFrame], float]`.
At each buy fill the engine invests `fraction * cash` and holds the remainder
as cash; sells exit the whole position. The callable receives only the bars
visible at the decision (index <= asof), preserving the no-lookahead
guarantee. Default None = current all-in behavior, byte-identical.

### 3. Validation harness — `scripts/backtest_vol_sizing.py`

Same IS/OOS protocol as `backtest_adaptive_dip.py` (half/half split, 420-day
warmup, sliced equity curves):

- Grid: `target_vol` in {10%, 15%, 20%, 25%, 30%} × application in
  {ST only, ST and DipRecovery}.
- Portfolio model: ST + DipRecovery sleeves (equal capital slices), matching
  the production stack. DipRecovery stop-exempt as in live.
- **Selection metric: Sharpe** (vol targeting trades raw return for
  risk-adjusted return; judging it on total return would always favor
  full-size).
- **Deployment gate:** best IS config must beat the unsized baseline's Sharpe
  out-of-sample. Total return and max drawdown reported alongside; a Sharpe
  win with catastrophic return give-up (> 1/3 of baseline return) is flagged
  for human review instead of auto-deploy.

### 4. Live wiring (only if validated)

- `_notional_for` multiplies buy notional by `vol_scale(bars, ...)`; bars are
  already available at the call site in `_prepare_signal`.
- Config: `vol_target` / `vol_floor` on the risk config, env-driven, default
  off (None) so deployment is an explicit config change.
- Per-strategy application follows the validation result (e.g., skip scaling
  when the position owner is DipRecovery, mirroring the stop-loss exemption).

## Error Handling

- `vol_scale` never raises in the hot path: bad inputs → 1.0 (current
  behavior).
- Engine validates the returned fraction (clamps to (0, 1]) so a buggy
  callable cannot produce leverage or a zero/negative position.

## Testing

- `vol_scale`: clamping at both ends, insufficient history → 1.0, NaN/zero vol
  → 1.0, monotonicity (higher vol → smaller scale).
- Engine: `entry_fraction=None` identical to today; constant fraction 0.5
  invests half and marks-to-market correctly; callable sees only visible bars
  (no-lookahead test); fraction outside (0, 1] is clamped.
- Pipeline (if wired): buys scaled, sells untouched, config off = unchanged.

## Success Criteria

Best in-sample (target_vol, application) config beats the unsized ST+Dip
sleeves baseline on out-of-sample Sharpe, with the return give-up flag clear —
or we keep flat sizing and record the negative result.

## Outcome (2026-06-10)

`scripts/backtest_vol_sizing.py --years 4` over AAPL, MSFT, NVDA, AMZN, GOOGL,
META, JPM, SPY, QQQ, TSLA:

- **In-sample (2022-06 → 2024-06):** best config (target_vol 15% on ST only)
  edged the baseline on Sharpe (1.23 vs 1.19, max_dd -20.6% vs -23.3%) but gave
  up 20 points of return (+72.8% vs +92.4%).
- **Out-of-sample (2024-06 → 2026-06):** the edge did not generalize — Sharpe
  0.79 vs baseline 0.87, return +30.5% vs +39.8%, drawdown only marginally
  better (-20.2% vs -22.6%).

**Decision: production keeps flat sizing (`cap_pct × free_cash`).**
Interpretation: ST's ADX gate and trend-break exit already avoid most
high-vol chop, and the 8% stop bounds entry risk — a vol overlay on top mostly
just dilutes winners. Notably, every grid config that scaled DipRecovery
("ST+DI") underperformed ST-only, re-confirming that DipRecovery's edge is
taking full-size high-vol dip risk.

The first validation run exposed a real engine bug (sell path overwrote cash,
destroying reserved capital under partial sizing — fixed with a regression
test). The `entry_fraction` engine support and `vol_scale` function stay in
the codebase (default off, tested) for future sizing experiments.
