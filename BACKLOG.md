# Backlog

---

## Correctness

### ~~Proposal Approval Race Condition~~ ✅ DONE
Atomic `UPDATE proposals SET status='approved' WHERE id=? AND status='pending'` via `try_approve_proposal()`.

### ~~Analysis Timeout~~ ✅ DONE
`asyncio.wait_for(..., timeout=120)` on LangChain call; yields error SSE event on timeout.

---

## Infrastructure

### ~~SQLite removal~~ ✅ DONE
All production paths (api, scheduler, scripts) now require `DATABASE_URL` (Supabase). SQLite kept in `sqlite_repo.py` for tests only.

### ~~Backend Auth Removal~~ ✅ DONE
Single-user app — `get_current_user()` returns `"admin"` unconditionally. Auth router and JWT removed.

### ~~Database Migrations (Alembic)~~ ✅ DONE
`alembic>=1.13` added. `migrations/versions/001_baseline.py` captures full schema (idempotent). Live DB stamped at `001`. `_run_migrations()` in FastAPI lifespan runs `alembic upgrade head` before schedulers start. Future schema changes: `alembic revision -m "desc"` + `op.execute("ALTER TABLE ...")` in the migration file.

---

## Trading Architecture

### ~~[CRITICAL] Broker-Side Stop-Loss Orders~~ ✅ DONE
GTC stop-market orders placed at Alpaca on every buy fill (`place_stop_order`). Cancelled before sell evaluation (`cancel_open_stops`). Stop price = `ref_price * (1 - stop_loss_pct)`.

### ~~[CRITICAL] Signal Timing Mismatch~~ ✅ DONE
Today's partial bar stripped from cache. Daily strategies now always evaluate against the previous confirmed close. Signal is stable across all 60s ticks within a day.

### ~~[HIGH] Bars Cache — Option A~~ ✅ DONE
Only completed bars cached (`end < today`). Live price fetched separately per tick via `get_live_prices_batch` (bid/ask midpoint). Cache keyed by calendar day; resets at midnight.

### ~~[HIGH] Limit Orders Instead of Market Orders~~ ✅ DONE
DAY limit at bid/ask mid for buys (`ORDER_TYPE=limit` env). Sells stay market. No fill by EOD → cancels; next tick retries.

### ~~[HIGH] Event-Driven Reconciliation~~ ✅ DONE
`AlpacaBroker.start_trade_stream()` runs `TradingStream` in a daemon thread. Every fill/cancel event invalidates a 5-minute `AccountState` cache. Between events, `reconcile()` returns cached state (0 API calls). After an event, next `reconcile()` does full API refresh. Stream started automatically in `_scheduler_loop`.

### ~~[MEDIUM] Universe Stability — Weekly Rescreen~~ ✅ DONE
Rescreen on Monday's first open tick. First run always rescreens. Held positions always retained even if screener drops them.

### ~~[MEDIUM] Transaction Cost Model in Position Sizing~~ ✅ DONE
`get_live_prices_batch` now returns (mids, spread_pcts). Gate check 0b rejects buys when 2×spread_pct > MAX_SPREAD_PCT (default 1%, env: MAX_SPREAD_PCT). `OrderIntent.spread_pct=0` → check skipped.

### ~~[MEDIUM] Pre-Market Signal Computation~~ ✅ DONE
`precompute_signals()` called on first post-close tick. `_prepare_signal` checks `_premarket_signals` cache before calling `strategy.generate()`. Cache keyed by (strategy_class, symbol, date). Crypto excluded.

### ~~[LOW] Correlation-Aware Sizing~~ ✅ DONE
`_correlation_factor()` computes 60-day rolling correlation vs all held positions. Buys in symbols with >0.7 corr to a held name get 0.5× size. Wired through Phase 2 pending_buys loop.

---

## ~~Intraday Strategies (make use of 60s poll)~~ ✅ DONE

All four intraday strategies implemented and wired. Enable via `INTRADAY_ALLOWLIST` env var (comma-separated symbols; keep **disjoint** from the daily equity universe). 60/40 capital split (daily/intraday). EOD force-exit ≥15 min before 4 PM ET. Per-day session-state reset. Broker positions are per-symbol so overlapping allowlists are unsupported.

### ~~Opening Range Breakout (ORB)~~ ✅ DONE
`trader/strategy/orb.py` — 1-min bars, 30-bar opening range, volume confirmation (1.5×), no re-entry after exit.

### ~~VWAP Mean Reversion~~ ✅ DONE
`trader/strategy/vwap_reversion.py` — 1-min bars, 2σ entry band, 20-bar std window.

### ~~Gap and Go~~ ✅ DONE
`trader/strategy/gap_and_go.py` — 1-min bars, entry window bars 5–9, gap ≥2%, volume ≥1.5×.

### ~~Intraday Trend (5-min Bars)~~ ✅ DONE
`trader/strategy/intraday_trend.py` — 5-min bars, SuperTrend (ATR=14, mult=3.0) with ADX≥20 regime filter.

---

## Audit Remediation — PR #12 (`fix/safety-validation-audit`)

Full audit 2026-07-11 (plan: `check-the-current-trading-linear-minsky.md`). P0 safety + P1 validation shipped in commit `d6b8cb1`; options enabled on paper. Remaining work → `docs/roadmap.md`.

### Safety & Correctness (P0) ✅ DONE
- **~~Sell fills unconfirmed~~** — outcome recorded only when `filled_order is not None` (uses `filled_order.filled_avg_price`); `_advance_state` skips owner pop on unconfirmed sells; new `reconcile_order_statuses(broker, repo, max_age_days=3)` upserts late-filled statuses. `pipeline.py`, `scheduler.py`, repo `get_orders_by_status`.
- **~~Autonomy toggle cosmetic~~** — file-backed `AutonomyOverride` in `gate.py` (mirrors `KillSwitch`); `effective_autonomy(config)` read at every decision gate; `controls.py` GET/POST rewired. Dashboard "manual" now actually halts auto trading.
- **~~No active account breaker~~** — `DAILY_LOSS_HALT_ENABLED=true` on Render/.env.example (code default stays False); breaker alert un-commented, gated on flag, once/day guard.
- **~~DipRecovery stop contradiction~~** — `stop_loss_multiplier` class attr on `Strategy` (DipRecovery=2.0 → 16% disaster stop, software + broker GTC at same widened level). Thesis breathes, catastrophe capped.
- **~~Fail-open startup/errors~~** — migration failure/timeout re-raises (aborts startup); `get_account_activities(raise_on_error=True)` propagates instead of silent `[]`; malformed JWT → always 401; auth debug logs demoted to DEBUG.

### Validation Rigor (P1) ✅ DONE
- **~~Permutation Sharpe inert~~** — replaced multiset shuffle with sign-flip test (H0: no directional edge); max-DD keeps order-shuffle.
- **~~Go-live gate stop-less~~** — `go_live_gate.py` passes `stop_loss_pct * stop_loss_multiplier`; engine now exits intra-bar (gap-down fills at open, not stop).
- **~~Sweep in-sample~~** — `param_sweep` supports `holdout_frac`; ranks on train, reports OOS metrics in `SweepResult`.
- **~~iid bootstrap~~** — `bootstrap_sharpe_ci` uses stationary block bootstrap (block ≈ n^(1/3)).
- **~~Crypto costs unrealistic~~** — `CostModel.taker_fee_bps` added; crypto backtests at 25bps. **Rerun deliverable still pending** (needs Alpaca keys) → `docs/roadmap.md`.

### Options Activation (P3.1) ✅ DONE
`OPTIONS_TRADING_ENABLED` / `WHEEL_STRATEGY_ENABLED` / `CSP_ON_DIP_ENABLED` = true on Render (requires `AUTONOMY=auto`). Cap held at 15% NAV (`MAX_OPTIONS_ALLOCATION_PCT`). Options depth (delta/roll/assignment) deferred → `docs/roadmap.md` P3.2.
