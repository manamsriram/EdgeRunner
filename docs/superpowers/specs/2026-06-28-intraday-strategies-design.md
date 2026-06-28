# Intraday Strategies Design

**Date:** 2026-06-28
**Status:** Approved

## Overview

Add four intraday strategies (ORB, VWAP Mean Reversion, Gap and Go, Intraday Trend) that run alongside existing daily strategies on the 60s scheduler tick. Each intraday strategy is flat by close. Intraday and daily strategies draw from separate capital pools and can hold the same symbol simultaneously.

---

## 1. Data Layer

### New function: `get_intraday_bars_batch`

**File:** `trader/data/alpaca_bars.py`

```python
get_intraday_bars_batch(
    symbols: list[str],
    timeframe: str,          # "1min" or "5min"
    lookback_minutes: int,   # 390 = full trading day
    config: Config | None = None,
) -> dict[str, pd.DataFrame]
```

- `"1min"` → `TimeFrame.Minute`
- `"5min"` → `TimeFrame(5, TimeFrameUnit.Minute)`
- Fetches from market open of current day (9:30 AM ET)
- Returns same OHLCV shape as daily bars — strategies are bar-agnostic
- No cache: today's intraday payload is small (~78 bars × N symbols for 5-min), re-fetch each tick is fine
- Called twice per tick from `run_pipeline` (once per timeframe group)

---

## 2. Strategy Base

### New class: `IntradayStrategy`

**File:** `trader/strategy/base.py`

```python
class IntradayStrategy(Strategy):
    pool = "intraday"
    eod_exit = True
    skip_fundamental_gate = True
    skip_overlay = True
    bar_timeframe: str = "5min"   # override per strategy: "1min" or "5min"
    lookback_minutes: int = 390
```

All 4 intraday strategies inherit this. One `isinstance(s, IntradayStrategy)` check in the pipeline gates all intraday-specific behavior — no scattered attribute checks.

---

## 3. Capital Pools

### `RiskLimits` changes (`trader/config.py`)

- **Add:** `intraday_pool_pct: float = 0.40` (env: `INTRADAY_POOL_PCT`)
- **Remove:** `max_trades_per_day` — eliminated entirely; PDT rule protection (`pdt_day_trade_limit`, `pdt_equity_threshold`) stays
- Daily pool fraction = `1.0 - intraday_pool_pct` (0.60 default)

### `AccountState` changes (`trader/execution/broker.py`)

- **Add:** `intraday_deployed: float = 0.0`
- `deployed_notional` tracks daily pool only (existing field, no rename)

### `position_owners` key change

Key changes from `symbol: str` to `(symbol, pool): tuple[str, str]`:

```python
# before
{"AAPL": "SuperTrend"}

# after
{("AAPL", "daily"): "SuperTrend", ("AAPL", "intraday"): "OpeningRangeBreakout"}
```

Ownership conflict checks only block within the same pool — both pools can hold AAPL simultaneously.

**DB migration required:** `position_owners` table (or equivalent column) must store `pool` alongside `symbol`. Add `pool VARCHAR(10) NOT NULL DEFAULT 'daily'` column via Alembic migration. On load, rows with no pool value default to `"daily"` for backward compat.

### Risk gate sizing (`trader/risk/gate.py`)

`_notional_for_side` uses pool equity, not total equity:

```python
pool_equity = equity * (intraday_pool_pct if pool == "intraday" else (1 - intraday_pool_pct))
max_position = pool_equity * max_position_pct
```

Example at $100k, 60/40, 10% max position:
- Daily max position: $6,000
- Intraday max position: $4,000

Remove all `max_trades_per_day` checks from gate and pipeline.

---

## 4. Pipeline Routing

### Pre-fetch (`run_pipeline`)

```python
# existing — unchanged
daily_syms = [s.symbol for s in strategies
              if not is_crypto_symbol(s.symbol) and not isinstance(s, IntradayStrategy)]
bars_cache = get_daily_bars_batch(daily_syms, start, end, config)

# new
intraday_caches: dict[str, dict[str, pd.DataFrame]] = {}
for tf in ("1min", "5min"):
    syms = [s.symbol for s in strategies
            if isinstance(s, IntradayStrategy) and s.bar_timeframe == tf]
    if syms:
        intraday_caches[tf] = get_intraday_bars_batch(syms, tf, 390, config)

# GapAndGo needs yesterday's close — fetch daily bars for those symbols too
gap_syms = [s.symbol for s in strategies if isinstance(s, GapAndGo)]
if gap_syms:
    gap_daily = get_daily_bars_batch(gap_syms, start, end, config)
    for s in strategies:
        if isinstance(s, GapAndGo) and s.symbol in gap_daily:
            s.prev_close = float(gap_daily[s.symbol]["close"].iloc[-1])
```

### `_prepare_signal` routing

```python
if isinstance(strategy, IntradayStrategy):
    bars = intraday_caches.get(strategy.bar_timeframe, {}).get(symbol)
    # skip fundamental gate
    # skip overlay (apply_overlay not called)
    # use (symbol, "intraday") for ownership checks
else:
    bars = bars_cache.get(symbol)  # existing path unchanged
```

### EOD exit

Checked inside `_prepare_signal` before signal generation:

```python
EOD_EXIT_MINUTES = int(os.getenv("EOD_EXIT_MINUTES", "15"))  # default 3:45 PM ET

if isinstance(strategy, IntradayStrategy) and strategy.eod_exit:
    market_close_utc = asof.replace(hour=20, minute=0, second=0)  # 4 PM ET = 20:00 UTC
    if asof >= market_close_utc - timedelta(minutes=EOD_EXIT_MINUTES):
        if symbol in state.positions and state.positions[symbol] > 0:
            signal = Signal(symbol, "sell", 1.0, "eod-exit: intraday flat before close")
```

EOD exit bypasses overlay and gate veto — same pattern as stop-loss. Position must close.

### `precompute_signals`

Add `isinstance(s, IntradayStrategy)` exclusion alongside the existing crypto exclusion. Intraday signals change tick-by-tick; precomputing them post-close is meaningless.

---

## 5. The Four Strategies

### 5.1 `IntradayTrend` (`trader/strategy/intraday_trend.py`)

```
bar_timeframe = "5min"
```

Reuses `supertrend()` and `adx()` from `trader/strategy/indicators.py` verbatim. Logic identical to `SuperTrend` daily strategy — buy when close is above SuperTrend line and ADX confirms trend; sell on SuperTrend line cross. The only difference is inheriting `IntradayStrategy` instead of `Strategy` and operating on 5-min bars. Exit by EOD or on reversal.

**Effort:** ~3h (SuperTrend logic already exists, mainly wiring)

### 5.2 `VWAPReversion` (`trader/strategy/vwap_reversion.py`)

```
bar_timeframe = "1min"
```

- VWAP = `cumsum(close × volume) / cumsum(volume)` — resets each day automatically (bars are today-only)
- Rolling std of `(close − vwap)` over last 20 bars
- **Buy:** `close < vwap − 2σ` (oversold, expect snap back)
- **Sell:** `close ≥ vwap` or EOD
- **Strength:** `min(deviation_in_σ / 3, 1.0)` — larger deviation = higher conviction
- `avg_volume`: mean of all bars' volume so far today
- Requires min 20 bars warm-up before signaling

**Effort:** ~4h

### 5.3 `GapAndGo` (`trader/strategy/gap_and_go.py`)

```
bar_timeframe = "1min"
```

- **Gap check:** `gap_pct = (bars.iloc[0]["open"] − prev_close) / prev_close`
- **Entry window:** bars 5–9 only (9:35–9:40 AM ET); miss window → hold rest of day
- **Buy condition:** `gap_pct > 0.02` AND `bars.iloc[0]["volume"] > 1.5 × avg_volume` AND price still above `prev_close` at entry bar
- `avg_volume`: 20-day rolling mean of first-bar volume (from daily bars — same daily cache used for `prev_close`)
- **Sell:** `close < entry_bar_open` (momentum faded) or EOD
- `prev_close` injected by pipeline from daily bars cache — no extra API call
- **State:** `_entered`, `_entry_bar_open`, `_entry_attempted` (reset via `warm_up()`)

**Effort:** ~4h

### 5.4 `OpeningRangeBreakout` (`trader/strategy/orb.py`)

```
bar_timeframe = "1min"
```

- **Range formation:** bars 0–29 (9:30–10:00 AM ET); `ORH = max(high[0:30])`, `ORL = min(low[0:30])`
- Range not set until bar index 29 exists — holds before then
- **Buy:** `close > ORH` AND `volume > 1.5 × avg_volume` AND only on first breakout (no re-entry after exit)
- `avg_volume`: mean of all bars' volume so far today
- **Sell:** `close < ORL` (range violated) or EOD
- **Strength:** `min((close − ORH) / ORH, 1.0)`
- **State:** `_orh`, `_orl`, `_range_set`, `_entered` (reset via `warm_up()`)

**Effort:** ~5h

---

## 6. Scheduler Registration

**File:** `trader/scheduler.py`

Intraday strategies are built per-symbol alongside daily strategies. Use a separate universe or a subset of the daily universe (high-volume, liquid names suitable for intraday momentum). Initially: same `DEFAULT_ALLOWLIST` symbols. New env var `INTRADAY_ALLOWLIST` to override.

Build function `_build_intraday_strategies_for(config, symbols)` analogous to existing `_build_strategies_for`.

---

## 7. Files Changed

| File | Change |
|------|--------|
| `trader/strategy/base.py` | Add `IntradayStrategy` base class |
| `trader/data/alpaca_bars.py` | Add `get_intraday_bars_batch` |
| `trader/config.py` | Add `intraday_pool_pct`; remove `max_trades_per_day` |
| `trader/execution/broker.py` | Add `intraday_deployed` to `AccountState`; update `position_owners` key type |
| `trader/risk/gate.py` | Pool-aware sizing; remove `max_trades_per_day` checks |
| `trader/pipeline.py` | Intraday bar routing; EOD exit; `(symbol, pool)` ownership; `GapAndGo.prev_close` injection; skip gate/overlay for intraday |
| `trader/scheduler.py` | `_build_intraday_strategies_for`; `INTRADAY_ALLOWLIST` env var |
| `trader/strategy/intraday_trend.py` | New strategy |
| `trader/strategy/vwap_reversion.py` | New strategy |
| `trader/strategy/gap_and_go.py` | New strategy |
| `trader/strategy/orb.py` | New strategy |
| Alembic migration | Add `pool` column to `position_owners` table |

---

## 8. Testing

Each strategy gets one `test_*.py` with:
- Signal correctness on synthetic intraday bars (buy/sell/hold cases)
- EOD exit trigger
- Boundary conditions (range not set yet, insufficient bars, no gap)

Infrastructure changes (capital pools, pipeline routing) tested via existing pipeline test harness with mock `IntradayStrategy` subclass.
