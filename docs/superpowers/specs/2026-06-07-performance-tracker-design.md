# Performance Tracker — Design Spec
**Date:** 2026-06-07  
**Status:** Approved

## Goal

Track live paper trading performance against hard thresholds to determine when the account is ready for real-money trading via Robinhood MCP. Output: CLI report + API endpoint + React dashboard page.

---

## Architecture

### Approach: New `trader/performance/` package (Option B)

Live performance metrics have a different data source (real Alpaca history + real Supabase fills) than backtests (synthetic bar replay). Keeping them in a dedicated module prevents confusion when live diverges from backtest.

### Data Flow

```
Alpaca API ──► get_portfolio_history()  → equity curve → Sharpe, drawdown, total_return
Supabase   ──► trades table             → FIFO-matched round-trips → win_rate, profit_factor
Alpaca bars ─► SPY daily bars           → benchmark_spy_return
Alpaca bars ─► BTC/USD daily bars       → benchmark_btc_return
                                                  ↓
                                           LiveMetrics dataclass
                                           GoLiveVerdict (PASS/FAIL + reasons)
```

---

## Files

| File | Purpose |
|------|---------|
| `trader/performance/__init__.py` | package marker |
| `trader/performance/metrics.py` | `LiveMetrics` dataclass + `compute_live_metrics()` |
| `api/routes/performance.py` | `GET /api/performance` FastAPI route |
| `scripts/performance_tracker.py` | CLI script, exit 0=PASS / 1=FAIL / 2=config err |
| `frontend/src/pages/Performance.tsx` | Dashboard page |
| `frontend/src/lib/api.ts` | Add `getPerformance()` + `PerformanceMetrics` type |
| `api/main.py` | Register performance router |
| `frontend/src/App.tsx` | Add `/performance` route + nav tab |

---

## `trader/performance/metrics.py`

### Thresholds

```python
MIN_SHARPE = 1.0
MAX_DRAWDOWN = -0.15   # negative convention (e.g. -0.09 = -9%)
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
    trade_count: int
    sharpe: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    total_return: float
    benchmark_spy_return: float | None
    benchmark_btc_return: float | None
    verdict: str                    # "PASS" | "FAIL" | "INSUFFICIENT_DATA"
    failing_checks: list[str]       # human-readable reasons for FAIL
    sample_size_warning: bool       # True if trade_count < MIN_TRADES or days < MIN_DAYS
    strategy_signals: dict[str, int]  # strategy_name -> signal count (V1, not P&L)
```

### `compute_live_metrics(config) -> LiveMetrics`

1. Fetch equity curve from `AlpacaBroker.get_portfolio_history()`
2. Compute Sharpe and max drawdown from equity series (reuse `_sharpe()` and `_max_drawdown()` logic from `trader/backtest/metrics.py`)
3. Compute days_active from first→last equity timestamp
4. Fetch all trades from `PostgresRepository` (or `SQLiteRepository` if no `DATABASE_URL`)
5. FIFO-match buy→sell per symbol to get round-trip P&L list
6. Compute win_rate = wins / total_trips; profit_factor = gross_profit / gross_loss
7. Fetch SPY bars via `get_daily_bars("SPY", ...)` over same date window; compute total return
8. Fetch BTC/USD bars (via `get_crypto_bars` or Alpaca crypto endpoint) over same window
9. Fetch strategy signal counts: `SELECT runs.strategy, COUNT(*) FROM signals JOIN runs ON signals.run_id = runs.id GROUP BY runs.strategy`
10. Run threshold checks → build `failing_checks` list → set verdict

**Insufficient data path:** If equity curve has < 2 points or trade_count == 0, return verdict `"INSUFFICIENT_DATA"` with zero metrics rather than crash.

**Benchmark failure path:** If SPY or BTC fetch fails (network, key error), set benchmark field to `None` — does not block verdict.

---

## `api/routes/performance.py`

- `GET /api/performance` — auth-gated via `get_current_user` dep
- 5-min in-process cache: module-level `_cache: dict` storing `{result, computed_at}`, recompute if `now - computed_at > 300s`
- On compute error: return `{"verdict": "INSUFFICIENT_DATA", ...}` with HTTP 200 (not 502); only 502 on unrecoverable config failure

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

Benchmark comparison
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

Exit codes: 0=PASS, 1=FAIL or INSUFFICIENT_DATA, 2=config error.

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

Benchmark Comparison (bar chart or table)
  Portfolio  +8.0%
  SPY        +5.0%
  BTC/USD   +14.0%

Strategy Signal Activity (V1)
  MomentumRSI   64
  MACrossover   31
  EMACrossover  15
  BollingerRev   8
```

### Color logic

- Metric tile border: green = threshold passing, red = failing, grey = insufficient data
- Verdict banner: green = PASS, red = FAIL, yellow = INSUFFICIENT_DATA
- Refetch interval: 5 min (matches server cache TTL)

### Empty state

If `verdict === "INSUFFICIENT_DATA"`, render: "Not enough paper trading data yet — run the scheduler in auto mode to populate."

### Nav

Add "Performance" tab to `App.tsx` nav. Route `/performance`. Matches existing tab style (Portfolio / Approvals / Controls).

---

## Out of Scope (V1)

- Per-strategy P&L attribution (orders don't carry strategy tag; deferred)
- Sortino ratio
- Monthly consistency breakdown
- Consecutive loss streak tracking
- Persisting historical metric snapshots to DB

---

## Testing

- Unit test `compute_live_metrics()` with injected fake broker + fake repo (no network)
- Test FIFO round-trip matching edge cases: partial fills, multiple open positions
- Test insufficient data path (empty equity curve, zero trades)
- Test threshold check logic independently (pure function, same pattern as `go_live_gate._check_thresholds`)
