# Realistic Return Plan

## Context

Original goal: ">=1% profit daily on initial capital." Reframed (2026-06-30) to
**maximize sustainable risk-adjusted return without blowup risk** — paper (Alpaca),
$25k+ equity (no PDT constraint).

**Why 1%/day is the wrong target:**
- 1%/day compounds to ~1,170%/yr (1.01^252). Best fund ever (Renaissance Medallion)
  ~66%/yr gross — you'd be asking ~18x that.
- EdgeRunner has **no leverage** — long/flat only, fully-invested-or-cash. Return
  scales only via trade frequency, position size, win rate, hold time. No knob
  manufactures 1%/day; forcing it = oversizing = maximizing probability of ruin.
- Return is an *output* of edge + sane sizing, not an input you tune to.
- Realistic ceiling with genuine edge: Sharpe ~1, ~15–40%/yr in a good regime.
  Anything claiming more is curve-fit.

Principle: **measure first, then change only what evidence supports.**

---

## Phase 0 — Honest baseline (NO code change; run existing scripts)

All have `main()`:
- `scripts/backtest_full.py` / `scripts/backtest_combos.py` — backtest live stack
  (SuperTrend, DipRecovery; Donchian crypto) → sharpe, sortino, calmar, max_drawdown,
  win_rate, IC/ICIR (`trader/backtest/metrics.py`).
- `scripts/go_live_gate.py` — OOS pass/fail (MIN_SHARPE 0.5, MAX_DD -25%, beat 80% of
  buy-and-hold, 60% of combos must pass).
- `scripts/paper_trading_report.py` / `scripts/performance_tracker.py` — live paper
  Sharpe, drawdown, win-rate, profit-factor.

Requires `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` in `.env`.

**Decision point:** if NO strategy clears the gate → bot has no validated edge yet;
no sizing/frequency tuning helps. Fix edge first. If some clear, scale only those.

**Statistical caveats (flaws in the existing gate):**
- `go_live_gate.py:27` `MIN_TRADE_COUNT=5` — 5 round-trips can't separate skill from
  luck. Treat a PASS on <~30 trades as *provisional only*; need ~30+ before scaling
  capital.
- `MIN_OOS_BARS=60` ≈ 3 months daily — thin. Combine multiple OOS splits.
- 60%-of-combos pass rule invites multiple-comparisons bias. Weight conviction toward
  strategies that pass across *many* symbols, not one lucky pair.

---

## Phase 1 — Capital preservation ✅ SHIPPED (2026-06-30)

Avoiding bad days beats catching good ones for compounding. Re-enabled the daily-loss
circuit (previously disabled as "too blunt") as a *smarter, opt-in* version.

**Changes:**
- `trader/config.py` — `RiskLimits.daily_loss_halt_enabled` (default False, env
  `DAILY_LOSS_HALT_ENABLED`); wired `DAILY_LOSS_LIMIT_PCT` (default 3%).
- `trader/risk/gate.py` — halt in the **buy-only** path (after sell branch):
  - Blocks new buys once `daily_pnl_pct <= -daily_loss_limit_pct`.
  - Sells / stop-loss exits never blocked (no trapping in a crashing position).
  - Skips when `daily_pnl_pct is None` (CCXT crypto has no `last_equity`) — never a
    hard reject, so unknown P&L can't freeze the book.
  - Equity-level daily; complements the per-position `stop_loss_pct` (8%).
- `tests/test_risk_gate.py` — 4 tests: off-by-default, blocks-buy-when-enabled,
  never-blocks-sells, skips-when-pnl-unknown.

**To activate:** set `DAILY_LOSS_HALT_ENABLED=true` (+ optional `DAILY_LOSS_LIMIT_PCT`)
on Render. Default off = prior behavior preserved.

---

## Phase 2 — Route capital to where edge is (evidence-gated, no code yet)

Bandit strategy weighting exists but shadow-only (`config.py:75-76`).
- First confirm shadow logs show the bandit's top-ranked strategies actually
  outperformed.
- If validated → set `BANDIT_WEIGHTING_LIVE=true` so capital ranks toward strategies
  with real measured IC instead of spreading evenly.
- If shadow data inconclusive → leave off. No evidence → no change.

---

## Phase 3 — More shots on goal (only for gate-passing strategies)

$25k+ removes PDT, so frequency is unlocked. With edge confirmed in Phase 0:
- `DYNAMIC_UNIVERSE=true` + raise `UNIVERSE_SIZE` (config.py:66-67) — more independent
  positive-edge bets = smoother equity curve (diversification), not bigger bets.
- Optionally raise `INTRADAY_POOL_PCT` (config.py:53) if intraday strategies clear the
  gate.
- Do NOT raise `max_position_pct` to chase return — concentration adds variance, not
  growth (Kelly: oversizing lowers long-run compounding).
- **Cost-reality gate before any intraday frequency increase:** backtest slippage
  defaults to 5bps (`costs.py:17`, hardcoded in `go_live_gate.py:70`). Optimistic for
  the low-priced momentum names the 1-min strategies (ORB, GapAndGo, VWAP) target
  (real spread+slippage often 20–50bps). Re-run backtests at `slippage_bps=20-30`
  before scaling any 1-min intraday strategy. (Crypto: model Alpaca fees ~10–25bps;
  equity commission $0 is correct.)

---

## Verification discipline

1. Phase 0 → record baseline Sharpe / win-rate / annualized return per strategy. The
   honest number to beat.
2. Phase 1 circuit unit-tested (see above). Run `rtk proxy venv/bin/python -m pytest`.
3. **Change ONE lever at a time** (P1 → P2 → P3, never bundled) so any P&L change is
   attributable. After each, re-run `paper_trading_report.py` vs baseline; promote only
   changes that improve *Sharpe*, not raw return.
4. **Realistic window:** daily strategies → ~5–20 trades/month, too few to conclude.
   Hold each change ~30+ trades / a full quarter. A good 2-week stretch is noise.
   Validate across at least one calm and one stressed regime
   (`trader/strategy/regime.py`).

---

## Deliberately NOT doing

- No leverage / margin (doesn't exist; the ruin path).
- No raising per-symbol position cap to chase the number.
- No re-enabling vol-targeting or regime-adaptive params (lost OOS already, 2026-06-10).
- No promise of 1%/day — abandoned as unachievable.

---

## Next steps (in order)

1. **Run Phase 0 baseline** — `go_live_gate.py` + `paper_trading_report.py`. Get the
   honest number. (Needs Alpaca keys.)
2. **Activate Phase 1 on Render** — `DAILY_LOSS_HALT_ENABLED=true` once comfortable.
3. **Commit Phase 1** — config + gate + tests (not yet committed).
4. **Fix unrelated overlay bug** — `test_overlay.py::test_strength_out_of_range_passthrough`
   fails: overlay applies approve action on out-of-range LLM strength instead of passing
   the original signal through. Regression from recent overlay caching/parsing work, not
   this change. Fix separately.
5. **Phase 2/3** — only after Phase 0 proves real edge.
