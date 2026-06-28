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

### Database Migrations (Alembic)
- **Risk:** Schema changes require manual SQL; new deploys break silently if schema is stale.
- **Fix:** Add `alembic`; capture current Postgres schema as baseline migration; run `alembic upgrade head` at startup.
- **Effort:** ~4 hr

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

### [HIGH] Event-Driven Reconciliation
- **Risk:** `broker.reconcile()` hits Alpaca positions API every 60s. Account state between fills is identical — polling wastes API calls.
- **Fix:** Subscribe to Alpaca trade update WebSocket. Update local state on fill/cancel. Full reconcile only on startup or after a gap.
- **Effort:** ~5 hr

### ~~[MEDIUM] Universe Stability — Weekly Rescreen~~ ✅ DONE
Rescreen on Monday's first open tick. First run always rescreens. Held positions always retained even if screener drops them.

### ~~[MEDIUM] Transaction Cost Model in Position Sizing~~ ✅ DONE
`get_live_prices_batch` now returns (mids, spread_pcts). Gate check 0b rejects buys when 2×spread_pct > MAX_SPREAD_PCT (default 1%, env: MAX_SPREAD_PCT). `OrderIntent.spread_pct=0` → check skipped.

### ~~[MEDIUM] Pre-Market Signal Computation~~ ✅ DONE
`precompute_signals()` called on first post-close tick. `_prepare_signal` checks `_premarket_signals` cache before calling `strategy.generate()`. Cache keyed by (strategy_class, symbol, date). Crypto excluded.

### ~~[LOW] Correlation-Aware Sizing~~ ✅ DONE
`_correlation_factor()` computes 60-day rolling correlation vs all held positions. Buys in symbols with >0.7 corr to a held name get 0.5× size. Wired through Phase 2 pending_buys loop.

---

## Intraday Strategies (make use of 60s poll)

Current daily-bar strategies fire once meaningfully per day — the 60s scheduler is wasted on them. These strategies are designed for intraday bars and benefit from frequent re-evaluation.

### Opening Range Breakout (ORB)
- **How it works:** First 30 minutes (9:30–10:00 AM ET) establishes the day's opening range (high/low). Buy when price breaks above range high with volume confirmation; sell at close or if price returns into range.
- **Why 60s poll:** Range forms over 30min; break needs to be caught within minutes. 60s tick is right granularity.
- **Data needed:** 1-min or 5-min intraday bars from Alpaca (already available via crypto bars path, needs equity equivalent)
- **Complements:** Existing daily strategies (ORB is intraday only, flat by close — no overnight risk)
- **Effort:** ~5 hr

### VWAP Mean Reversion
- **How it works:** When price deviates >2σ from VWAP, fade the move expecting reversion. Exit at VWAP or end of day. Works best on high-volume liquid names.
- **Why 60s poll:** VWAP and σ bands update every tick — signal is only valid in real-time.
- **Data needed:** Intraday bars + rolling VWAP computation (no external data source needed, computed from bars)
- **Complements:** ORB (ORB is trend-following; VWAP reversion fades overextended moves — opposite regimes)
- **Effort:** ~4 hr

### Gap and Go
- **How it works:** Pre-market gap >2% on volume. At 9:35 AM, confirm gap is holding (price above prior close + volume). Enter long, ride momentum for first 30–60 min, exit before 11 AM.
- **Why 60s poll:** Entry window is narrow (9:30–9:45 AM). Miss it = bad fill. 60s is the right frequency.
- **Data needed:** Pre-market quote (Alpaca supports this) + first few 1-min bars at open
- **Complements:** ORB (gap-and-go is a special case of ORB where the range is already set pre-market)
- **Effort:** ~4 hr

### Intraday Trend (5-min Bars)
- **How it works:** Identify trend direction from first hour (EMA or SuperTrend on 5-min bars). Ride in trend direction, stop out on reversal signal. Exit by 3:30 PM to avoid closing auction noise.
- **Why 60s poll:** 5-min bar closes every 5 ticks — scheduler naturally aligns. Signal updates as new bars print.
- **Data needed:** 5-min intraday bars from Alpaca (same API, different `TimeFrame`)
- **Complements:** Reuses existing `SuperTrend` logic — just needs a 5-min bar feed wired in instead of daily
- **Effort:** ~3 hr (SuperTrend already exists, mainly wiring)
