# Performance Tracker — Design Spec
**Date:** 2026-06-07  
**Status:** Approved (revised after senior review)

## Goal

Track live paper trading performance against hard thresholds to determine when the account is ready for real-money trading via Robinhood MCP. Output: CLI report + API endpoint + React dashboard page.

---

## Architecture

### Approach: New `trader/performance/` package

Live performance metrics have a different data source (real Alpaca history + real Alpaca fills) than backtests (synthetic bar replay). Keeping them in a dedicated module prevents confusion when live diverges from backtest.

### Data Flow

```
Alpaca portfolio history (period="1A") → equity curve → Sharpe, max_drawdown, total_return
Alpaca account activities (type=FILL)  → actual fills → win_rate, profit_factor (FIFO)
Alpaca bars SPY (same date window)     → benchmark_spy_return
Alpaca bars BTC/USD (same window)      → benchmark_btc_return
Supabase runs table MIN(started_at)    → days_active (authoritative — not equity window)
Supabase signals JOIN runs             → strategy signal counts (per strategy class name)
                                                  ↓
                                           LiveMetrics dataclass
                                           GoLiveVerdict (PASS/FAIL/INSUFFICIENT_DATA)
```

**Why fills from Alpaca, not DB trades table:** `record_trade()` exists in the repo schema but is
never called in the pipeline. The trades table is empty. Alpaca's
`/v2/account/activities?activity_type=FILL` returns all fills with symbol, side, qty, price —
sufficient for FIFO P&L matching without any pipeline changes.

---

## Files Changed / Added

| File | Change |
|------|--------|
| `trader/performance/__init__.py` | new — package marker |
| `trader/performance/metrics.py` | new — `LiveMetrics` dataclass + `compute_live_metrics()` |
| `trader/execution/broker.py` | extend — add `get_account_activities()` + `period` param to `get_portfolio_history()` |
| `api/routes/performance.py` | new — `GET /api/performance` FastAPI route |
| `scripts/performance_tracker.py` | new — CLI script |
| `frontend/src/pages/Performance.tsx` | new — dashboard page |
| `frontend/src/lib/api.ts` | extend — add `getPerformance()` + `PerformanceMetrics` type |
| `api/main.py` | extend — register performance router |
| `frontend/src/App.tsx` | extend — add `/performance` route + nav tab |

---

## `trader/execution/broker.py` changes

### `get_portfolio_history(period="1A")`

Pass `period` through to the Alpaca SDK. Default `"1A"` (1 year) gives enough history for
Sharpe/drawdown to be meaningful. Caller can override. Also update the `_TradingClient` Protocol
to accept an optional period argument.

### `get_account_activities(activity_type="FILL") -> list[dict]`

New method. Returns fills as plain dicts:
```python
{
    "symbol": "AAPL",
    "side": "buy",          # "buy" | "sell"
    "qty": 1.5,
    "price": 182.34,
    "ts": "2026-05-01T14:32:00Z"
}
```
Add to `_TradingClient` Protocol. Any error returns `[]` (same fail-safe pattern as `get_positions`).

---

## `trader/performance/metrics.py`

### Thresholds

```python
MIN_SHARPE = 1.0
MAX_DRAWDOWN = -0.15      # stored negative (e.g. -0.09 = -9%)
MIN_PROFIT_FACTOR = 1.5
MIN_WIN_RATE = 0.45
MIN_TRADES = 100
MIN_DAYS = 60
```

### `LiveMetrics` dataclass

```python
@dataclass(frozen=True)
class LiveMetrics:
    days_active: int
    trade_count: int           # round-trip count from Alpaca fills
    sharpe: float
    max_drawdown: float
    win_rate: float
    profit_factor: float       # inf if no losing trades; 0.0 if no winning trades
    total_return: float
    benchmark_spy_return: float | None    # None if fetch failed
    benchmark_btc_return: float | None    # None if fetch failed
    verdict: str               # "PASS" | "FAIL" | "INSUFFICIENT_DATA"
    failing_checks: list[str]  # human-readable threshold failures (includes sample size)
    strategy_signals: dict[str, int]  # strategy class name → signal count
```

`sample_size_warning` removed — covered by `failing_checks` ("only N trades, need ≥100").

### `compute_live_metrics(config, broker, repo) -> LiveMetrics`

Parameters are injected so the function is unit-testable without network.

1. **Equity curve:** `broker.get_portfolio_history(period="1A")` → `pd.Series` of daily equity values
2. **Sharpe + max_drawdown:** computed directly (duplicate the ~10 lines from `backtest/metrics.py`; do NOT import private `_sharpe`/`_max_drawdown` — they are internal to backtest)
3. **Total return:** `(end_equity / start_equity) - 1.0`
4. **days_active:** `SELECT MIN(started_at) FROM runs WHERE mode='auto'` via `repo` — more authoritative than equity curve window
5. **Fills:** `broker.get_account_activities(activity_type="FILL")` → sorted by ts per symbol
6. **FIFO round-trips:** match buys → sells per symbol. Each complete round-trip = `(sell_price − buy_price) × qty`. Partial fills (open positions) are excluded.
7. **win_rate:** `wins / total_round_trips`
8. **profit_factor:** `gross_profit / gross_loss`. Guards: if `gross_loss == 0` and `gross_profit > 0`, return `float('inf')`; if both zero (no closed trades), return `0.0`
9. **SPY benchmark:** `get_daily_bars("SPY", start=equity_start, end=equity_end)` → total return
10. **BTC benchmark:** Alpaca crypto bars for `"BTC/USD"` over same window (not CCXT — market data only, no exchange routing)
11. **Strategy signals:** `SELECT runs.strategy, COUNT(*) FROM signals JOIN runs ON signals.run_id = runs.id WHERE runs.mode='auto' GROUP BY runs.strategy`
12. **Verdict:** check all 6 thresholds → collect failures → PASS if none; FAIL if any; INSUFFICIENT_DATA only if equity curve has < 2 points (truly no data)

### Benchmark gating

Benchmarks (SPY, BTC) are **informational only** — not gated in verdict. Beating SPY is a useful signal but a 60-day paper window can appear to beat SPY by luck. The 6 numeric thresholds are the hard gate. Benchmark numbers appear in the report as context.

### Profit factor edge cases

| Situation | Returned value |
|-----------|---------------|
| Some wins, some losses | `gross_profit / gross_loss` |
| All wins, no losses | `float('inf')` — displayed as "∞" in UI |
| No closed round-trips | `0.0` → triggers INSUFFICIENT_DATA if trade_count == 0 |

---

## `api/routes/performance.py`

- `GET /api/performance` — auth-gated via `get_current_user`
- **5-min in-process cache:** module-level `dict` storing `{result, computed_at}`. Recompute if stale. Acceptable for single Render dyno; multiple instances just do independent fetches (no correctness issue).
- **Blocking calls:** `compute_live_metrics` makes 4–5 sync network calls. Run in `asyncio.run_in_executor(None, ...)` to avoid blocking the event loop — same pattern should be applied to the existing portfolio routes too, but that's out of scope here.
- **On compute error:** HTTP 200 with `{"verdict": "INSUFFICIENT_DATA", ...}` — not 502. Only 502 on missing config (no Alpaca keys).

---

## `scripts/performance_tracker.py`

CLI report format:

```
============================================================
Live Paper Trading — Performance Report
============================================================
Days active     :  72  (threshold ≥60)       ✓
Trades          : 118  (threshold ≥100)      ✓
Sharpe          : 1.24 (threshold ≥1.00)     ✓
Max drawdown    : -9.1% (threshold ≤15%)     ✓
Win rate        : 51.0% (threshold ≥45%)     ✓
Profit factor   : 1.73 (threshold ≥1.50)     ✓

Benchmark comparison  (informational — not gated)
  Portfolio     :  +8.0%
  SPY           :  +5.0%
  BTC/USD       : +14.0%

Strategy signals (V1 — counts only, not P&L)
  MomentumRSI   : 64 signals
  MACrossover   : 31 signals
  EMACrossover  : 15 signals
  BollingerRev  :  8 signals
============================================================
GO-LIVE VERDICT: PASS
============================================================
```

Exit codes: 0=PASS, 1=FAIL or INSUFFICIENT_DATA, 2=config error (no Alpaca keys).

---

## Frontend — `Performance.tsx`

### Layout

```
┌─────────────────────────────────────────────────────────┐
│  GO-LIVE VERDICT: PASS                  (green banner)  │
└─────────────────────────────────────────────────────────┘

┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│  Sharpe  │ │ Max DD   │ │ Win Rate │ │  Profit  │
│   1.24   │ │  -9.1%   │ │  51.0%  │ │  Factor  │
│  ✓ ≥1.0  │ │ ✓ ≤15%  │ │  ✓ ≥45% │ │   1.73   │
└──────────┘ └──────────┘ └──────────┘ └──────────┘
┌──────────┐ ┌──────────┐
│  Trades  │ │   Days   │
│   118    │ │    72    │
│ ✓ ≥100   │ │  ✓ ≥60  │
└──────────┘ └──────────┘

Benchmark Comparison  (informational)
  Portfolio  +8.0%   ████████████
  SPY        +5.0%   ████████
  BTC/USD   +14.0%   ████████████████████

Strategy Signal Activity (V1)
  MomentumRSI   64  ██████████████
  MACrossover   31  ███████
  EMACrossover  15  ████
  BollingerRev   8  ██
```

### Color logic

- Metric tile border: green = threshold passing, red = failing, grey = no data
- Verdict banner: green = PASS, red = FAIL, yellow = INSUFFICIENT_DATA
- profit_factor displayed as "∞" when `Infinity`
- Benchmark rows: no pass/fail coloring (informational)
- Refetch interval: 5 min (matches cache TTL)

### Empty state

If `verdict === "INSUFFICIENT_DATA"`: "Not enough paper trading data yet — run the scheduler in auto mode to populate."

### Nav

Add "Performance" tab to `App.tsx` nav. Route `/performance`. Matches existing tab style.

---

## Out of Scope (V1)

- Per-strategy P&L attribution (orders don't carry strategy tag; deferred to when `record_trade` is wired)
- Sortino ratio
- Monthly consistency breakdown
- Consecutive loss streak tracking
- Persisting historical metric snapshots to DB
- Wiring `record_trade()` into the pipeline fill path (deferred — Alpaca fills API is sufficient for now)

---

## Testing

- Unit test `compute_live_metrics()` with injected fake broker + fake repo (zero network)
- Test FIFO round-trip matching: partial fills (open positions excluded), multiple symbols, mixed buy/sell ordering
- Test profit_factor edge cases: all wins (→ inf), all losses (→ 0.0 with FAIL), no closed trades
- Test insufficient data path: empty equity curve → INSUFFICIENT_DATA, not crash
- Test threshold check logic as a pure function (same pattern as `go_live_gate._check_thresholds`)
- Test `get_account_activities` broker method with injected fake client
