# Backlog

---

## Correctness

### Proposal Approval Race Condition
- **Risk:** Two simultaneous approve clicks could submit duplicate orders (window between `_get_pending_proposal()` and `set_proposal_status()`)
- **Fix:** Single atomic `UPDATE proposals SET status='approved' WHERE id=? AND status='pending'`; check `cursor.rowcount == 1`, reject 409 if 0
- **Effort:** ~1 hr

### Analysis Timeout
- **Risk:** LangChain agent hangs → SSE stream open indefinitely (zombie connection)
- **Fix:** Wrap `loop.run_in_executor(None, Analyze_stock, body.query)` with `asyncio.wait_for(..., timeout=120)`; yield error SSE event on `asyncio.TimeoutError`
- **Effort:** ~20 min

---

## Infrastructure

### Database Migrations (Alembic)
- **Risk:** New deployments fail silently if schema hasn't been pre-created; schema changes require manual SQL
- **Fix:** Add `alembic`; convert inline `CREATE TABLE` in `SQLiteRepository` to migration scripts; run `alembic upgrade head` at startup
- **Effort:** ~4 hr

---

## Trading Architecture

### [CRITICAL] Broker-Side Stop-Loss Orders
- **Risk:** Stops live entirely in software. Render OOM kill, deploy restart, or any process crash between ticks = stop-loss coverage gone. Position can gap down 20% overnight with no protection.
- **Fix:** On every buy fill, immediately place a GTC stop-limit order at Alpaca at the entry bar low. On sell fill or time-exit, cancel the GTC stop. App-level quick-exit becomes secondary check, not primary safety net.
- **Effort:** ~3 hr

### [CRITICAL] Signal Timing Mismatch (Daily Strategies on 60s Loop)
- **Risk:** SuperTrend, DipRecovery, DonchianBreakout are daily-close strategies. Re-evaluating every 60s against a partial intraday bar = acting on noise the strategies weren't designed for.
- **Fix:** Compute daily signals once per day against the previous confirmed close (`end = yesterday 4:00 PM ET`). Cache the decision for the day. 60s loop only re-checks exit conditions against live price — not entry signals.
- **Effort:** ~4 hr

### [HIGH] Bars Cache — Complete Option A
- **Risk:** Current cache freezes today's partial bar at first-tick time. Exit quick-check uses `bars.iloc[-1].close` as live price — stale after first tick.
- **Fix:** Cache completed bars only (`end = yesterday`). Fetch today's single bar (or live quote) fresh each tick for exit checks. Eliminates 99% of allocation cost while keeping live price awareness.
- **Effort:** ~2 hr

### [HIGH] Limit Orders Instead of Market Orders
- **Risk:** Market orders on Alpaca IEX feed guarantee slippage, compounds at scale.
- **Fix:** Limit orders at mid-price (bid+ask)/2 with short IOC timeout, fall back to market if unfilled. Add `ORDER_TYPE` config flag — paper trading stays market until ready.
- **Effort:** ~3 hr

### [HIGH] Event-Driven Reconciliation
- **Risk:** `broker.reconcile()` hits Alpaca positions API every 60s. Account state between fills is identical — polling wastes API calls and memory.
- **Fix:** Subscribe to Alpaca trade update WebSocket. Update local state on fill/cancel events. Full reconcile only on startup or after a gap.
- **Effort:** ~5 hr

### [MEDIUM] Universe Stability — Weekly Rescreen
- **Risk:** Daily universe rebuilds cause turnover (dropped symbols = orphan positions, new symbols = cold-start strategies with no warm-up). Live behavior diverges from backtests.
- **Fix:** Rescreen weekly (Monday pre-market). Mid-week additions only for held positions losing eligibility.
- **Effort:** ~2 hr

### [MEDIUM] Transaction Cost Model in Position Sizing
- **Risk:** `vol_scale` sizes on volatility alone — ignores spread cost, meaningful for low-liquidity screened symbols.
- **Fix:** Estimate round-trip cost (spread + 2× slippage). Reduce size or skip if expected cost > X% of expected edge.
- **Effort:** ~3 hr

### [MEDIUM] Pre-Market Signal Computation
- **Risk:** First tick at market open computes signals + fetches bars + places orders simultaneously — latency spike right when execution matters most.
- **Fix:** Run signal-only pipeline pass at 4:15 PM ET on previous close data. Cache decisions. Market-open tick only executes pre-computed decisions + checks exits.
- **Effort:** ~3 hr

### [LOW] Correlation-Aware Sizing
- **Risk:** Two highly correlated symbols (e.g. NVDA + AMD) each get full vol-targeted size — effective concentration doubles without the risk gate knowing.
- **Fix:** At Phase 2 buy-ranking time, reduce size for symbols with >0.7 rolling correlation to an already-held position.
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
