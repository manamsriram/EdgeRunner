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

### [HIGH] Limit Orders Instead of Market Orders
- **Risk:** Market orders on Alpaca IEX feed guarantee slippage, compounds at scale.
- **Fix:** DAY limit at bid/ask mid for buys (`ORDER_TYPE=limit` env flag). Sells stay market — reliability > price improvement for exits. If limit doesn't fill, cancels at EOD; next tick retries with fresh mid price. No IOC/fallback complexity.
- **Effort:** ~2 hr

### [HIGH] Event-Driven Reconciliation
- **Risk:** `broker.reconcile()` hits Alpaca positions API every 60s. Account state between fills is identical — polling wastes API calls.
- **Fix:** Subscribe to Alpaca trade update WebSocket. Update local state on fill/cancel. Full reconcile only on startup or after a gap.
- **Effort:** ~5 hr

### [MEDIUM] Universe Stability — Weekly Rescreen
- **Risk:** Daily universe rebuilds cause turnover (dropped symbols = orphan positions, new symbols = cold-start strategies with no warm-up).
- **Fix:** Rescreen weekly (Monday pre-market). Mid-week changes only for positions losing eligibility.
- **Effort:** ~2 hr

### [MEDIUM] Transaction Cost Model in Position Sizing
- **Risk:** `vol_scale` sizes on volatility alone — ignores spread cost, meaningful for low-liquidity screened symbols.
- **Fix:** Estimate round-trip cost (spread + 2× slippage). Reduce size or skip if expected cost > X% of expected edge.
- **Effort:** ~3 hr

### [MEDIUM] Pre-Market Signal Computation
- **Risk:** First tick at market open computes signals + fetches bars + places orders simultaneously — latency spike when execution matters most.
- **Fix:** Run signal-only pipeline pass at 4:15 PM ET on previous close data. Cache decisions. Market-open tick only executes pre-computed decisions + checks exits.
- **Effort:** ~3 hr

### [LOW] Correlation-Aware Sizing
- **Risk:** Two highly correlated symbols (e.g. NVDA + AMD) each get full vol-targeted size — effective concentration doubles without the risk gate knowing.
- **Fix:** At buy-ranking time, reduce size for symbols with >0.7 rolling correlation to an already-held position.
- **Effort:** ~4 hr

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
