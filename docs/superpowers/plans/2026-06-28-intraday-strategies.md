# Intraday Strategies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four intraday strategies (IntradayTrend, VWAPReversion, GapAndGo, OpeningRangeBreakout) that run on the existing 60s scheduler, draw from a separate 40% capital pool, and are flat by end of day.

**Architecture:** `IntradayStrategy(Strategy)` base class gates all intraday-specific pipeline behaviour via a single `isinstance` check. Capital is split 60% daily / 40% intraday; `AccountState` tracks each pool's deployed notional separately. The `position_owners` DB table and in-memory dict gain a `pool` column so both pools can hold the same symbol simultaneously.

**Tech Stack:** Python, alpaca-py (`TimeFrame.Minute`, `TimeFrameUnit`), pandas, zoneinfo, pytest, Alembic (raw SQL migrations).

## Global Constraints

- Python 3.11+; `zoneinfo` stdlib (no `pytz`)
- alpaca-py for all bar fetching; `DataFeed.IEX`; `Adjustment.ALL` on daily bars; no adjustment on intraday bars
- All new strategy files in `trader/strategy/`; test files in `tests/`
- `AccountState` lives in `trader/risk/gate.py` (not broker.py)
- No comments except where the WHY is non-obvious
- No new dependencies
- TDD: write failing test first, then implementation

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `trader/strategy/base.py` | Modify | Add `IntradayStrategy` base class |
| `trader/data/alpaca_bars.py` | Modify | Add `get_intraday_bars_batch`, `_to_intraday_frame` |
| `trader/config.py` | Modify | Add `intraday_pool_pct`; remove `max_trades_per_day` |
| `trader/risk/gate.py` | Modify | Add `intraday_deployed` to `AccountState`; add `pool` to `OrderIntent`; pool-aware sizing in check 6 |
| `trader/portfolio/repository.py` | Modify | Update abstract signatures: `get_position_owners` return type, `set/clear_position_owner` pool param |
| `trader/portfolio/postgres_repo.py` | Modify | Implement updated signatures |
| `trader/portfolio/sqlite_repo.py` | Modify | Implement updated signatures |
| `migrations/versions/002_position_owners_pool.py` | Create | Add `pool` column; change PK to `(symbol, pool)` |
| `trader/pipeline.py` | Modify | Intraday bar pre-fetch; `_prepare_signal` branching + EOD exit; pool-aware `_advance_state` and `_notional_for`; `precompute_signals` exclusion |
| `trader/strategy/intraday_trend.py` | Create | IntradayTrend strategy (5min SuperTrend) |
| `trader/strategy/vwap_reversion.py` | Create | VWAPReversion strategy (1min VWAP mean reversion) |
| `trader/strategy/gap_and_go.py` | Create | GapAndGo strategy (1min gap momentum) |
| `trader/strategy/orb.py` | Create | OpeningRangeBreakout strategy (1min ORB) |
| `trader/scheduler.py` | Modify | Add `_build_intraday_strategies_for`; register intraday stack |
| `tests/test_intraday_base.py` | Create | Tests for `IntradayStrategy` + `get_intraday_bars_batch` |
| `tests/test_capital_pools.py` | Create | Tests for pool-aware gate + AccountState |
| `tests/test_intraday_trend.py` | Create | Tests for IntradayTrend |
| `tests/test_vwap_reversion.py` | Create | Tests for VWAPReversion |
| `tests/test_gap_and_go.py` | Create | Tests for GapAndGo |
| `tests/test_orb.py` | Create | Tests for OpeningRangeBreakout |

---

## Task 1: IntradayStrategy base class + intraday bar fetching

**Files:**
- Modify: `trader/strategy/base.py`
- Modify: `trader/data/alpaca_bars.py`
- Create: `tests/test_intraday_base.py`

**Interfaces:**
- Produces: `IntradayStrategy` importable from `trader.strategy.base`; flags `pool="intraday"`, `eod_exit=True`, `skip_fundamental_gate=True`, `skip_overlay=True`, `bar_timeframe="5min"`, `lookback_minutes=390`
- Produces: `get_intraday_bars_batch(symbols, timeframe, lookback_minutes, config) -> dict[str, pd.DataFrame]` in `trader.data.alpaca_bars`; returns minute-level DatetimeIndex (NOT normalized to daily dates)

---

- [ ] **Step 1: Write failing tests**

```python
# tests/test_intraday_base.py
from __future__ import annotations
import pandas as pd
import pytest
from trader.strategy.base import IntradayStrategy, Signal


class _Stub(IntradayStrategy):
    def _decide(self, bars, asof):
        return Signal(self.symbol, "hold", 0.0, "stub")


def test_intraday_strategy_flags():
    s = _Stub("AAPL")
    assert s.pool == "intraday"
    assert s.eod_exit is True
    assert s.skip_fundamental_gate is True
    assert s.skip_overlay is True
    assert s.bar_timeframe == "5min"
    assert s.lookback_minutes == 390


def test_intraday_strategy_override_timeframe():
    class _OnMin(IntradayStrategy):
        bar_timeframe = "1min"
        def _decide(self, bars, asof):
            return Signal(self.symbol, "hold", 0.0, "stub")
    s = _OnMin("AAPL")
    assert s.bar_timeframe == "1min"


def test_intraday_bars_index_is_minute_level():
    """_to_intraday_frame must NOT normalize the index to dates."""
    import numpy as np
    idx = pd.date_range("2024-01-15 09:30", periods=30, freq="1min")
    raw = pd.DataFrame({
        "open": np.ones(30), "high": np.ones(30),
        "low": np.ones(30), "close": np.ones(30), "volume": np.ones(30),
    }, index=idx)
    from trader.data.alpaca_bars import _to_intraday_frame
    result = _to_intraday_frame(raw, "AAPL")
    assert result.index.dtype != "datetime64[ns]" or result.index[0].hour != 0, \
        "index must keep intraday timestamps, not normalize to midnight"
    assert result.index[0].hour == 9
    assert result.index[0].minute == 30
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
venv/bin/python -m pytest tests/test_intraday_base.py -v
```
Expected: FAIL — `IntradayStrategy` not defined, `_to_intraday_frame` not defined.

- [ ] **Step 3: Add `IntradayStrategy` to `trader/strategy/base.py`**

Append after the `Strategy` class (before `PairStrategy`):

```python
class IntradayStrategy(Strategy):
    """Base for intraday strategies. Pipeline routes via isinstance check."""
    pool: str = "intraday"
    eod_exit: bool = True
    skip_fundamental_gate: bool = True
    skip_overlay: bool = True
    bar_timeframe: str = "5min"   # override per subclass: "1min" or "5min"
    lookback_minutes: int = 390
```

- [ ] **Step 4: Add `_to_intraday_frame` and `get_intraday_bars_batch` to `trader/data/alpaca_bars.py`**

Append at the bottom of the file:

```python
def _to_intraday_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Normalise alpaca-py MultiIndex frame for intraday bars.

    Unlike _to_frame, does NOT normalize to daily dates — minute timestamps preserved.
    """
    if raw is None or raw.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = raw
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    df = df.rename(columns=str.lower)[BAR_COLUMNS].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "timestamp"
    return df.sort_index()


def get_intraday_bars_batch(
    symbols: list[str],
    timeframe: str,
    lookback_minutes: int = 390,
    config: "Config | None" = None,
) -> dict[str, pd.DataFrame]:
    """Fetch intraday bars for many symbols in one API call. No cache.

    timeframe: "1min" → TimeFrame.Minute; "5min" → TimeFrame(5, TimeFrameUnit.Minute)
    lookback_minutes: bars fetched from (now - lookback_minutes); 390 = full trading day.
    Returns {symbol: DataFrame} with minute-level DatetimeIndex.
    """
    from datetime import timezone as _tz
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    if not symbols:
        return {}

    config = config or load_config()
    config.require_alpaca_credentials()

    if timeframe == "1min":
        tf = TimeFrame.Minute
    elif timeframe == "5min":
        tf = TimeFrame(5, TimeFrameUnit.Minute)
    else:
        raise ValueError(f"unsupported intraday timeframe: {timeframe!r}")

    now = datetime.now(_tz.utc)
    start = now - timedelta(minutes=lookback_minutes + 10)

    client = StockHistoricalDataClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
    )
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=tf,
        start=start,
        end=now,
        feed=DataFeed.IEX,
    )
    try:
        bars = client.get_stock_bars(request)
    except Exception:
        logger.warning("intraday bar fetch failed for %d symbols", len(symbols))
        return {}

    result: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            result[sym] = _to_intraday_frame(bars.df, sym)
        except Exception:
            logger.warning("no intraday bar data for %s — skipping", sym)
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
venv/bin/python -m pytest tests/test_intraday_base.py -v
```
Expected: PASS (3 tests).

- [ ] **Step 6: Run full test suite to check for regressions**

```bash
venv/bin/python -m pytest --tb=short -q
```
Expected: all existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add trader/strategy/base.py trader/data/alpaca_bars.py tests/test_intraday_base.py
git commit -m "feat(intraday): IntradayStrategy base class + intraday bar fetching"
```

---

## Task 2: Capital pools — config, AccountState, OrderIntent, gate sizing

**Files:**
- Modify: `trader/config.py`
- Modify: `trader/risk/gate.py`
- Create: `tests/test_capital_pools.py`

**Interfaces:**
- Consumes: nothing from Task 1
- Produces: `RiskLimits.intraday_pool_pct: float = 0.40` (env: `INTRADAY_POOL_PCT`); `max_trades_per_day` removed
- Produces: `AccountState.intraday_deployed: float = 0.0`
- Produces: `OrderIntent.pool: str = "daily"` — gate uses this for pool-aware position cap

---

- [ ] **Step 1: Write failing tests**

```python
# tests/test_capital_pools.py
from __future__ import annotations
import pytest
from trader.risk.gate import AccountState, OrderIntent, RiskDecision, RiskGate, KillSwitch
from trader.config import RiskLimits


def _state(**kw) -> AccountState:
    defaults = dict(
        equity=100_000.0, positions={}, open_order_symbols=frozenset(),
        trades_today=0, daily_pnl_pct=0.0, cash=100_000.0,
    )
    defaults.update(kw)
    return AccountState(**defaults)


def _limits(**kw) -> RiskLimits:
    defaults = dict(
        allowlist=None, max_position_pct=0.10,
        pdt_equity_threshold=25_000, pdt_day_trade_limit=3,
        intraday_pool_pct=0.40,
    )
    defaults.update(kw)
    return RiskLimits(**defaults)


def test_intraday_pool_pct_default():
    limits = RiskLimits()
    assert limits.intraday_pool_pct == 0.40


def test_max_trades_per_day_removed():
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(RiskLimits)}
    assert "max_trades_per_day" not in field_names


def test_account_state_has_intraday_deployed():
    state = _state()
    assert state.intraday_deployed == 0.0


def test_order_intent_default_pool_is_daily():
    intent = OrderIntent(symbol="AAPL", side="buy", notional=1000.0, ref_price=150.0)
    assert intent.pool == "daily"


def test_order_intent_pool_intraday():
    intent = OrderIntent(symbol="AAPL", side="buy", notional=1000.0, ref_price=150.0, pool="intraday")
    assert intent.pool == "intraday"


def test_gate_daily_pool_uses_daily_equity_fraction():
    """Daily cap = max_position_pct * equity * (1 - intraday_pool_pct) = 10% * 60k = $6,000."""
    limits = _limits()
    gate = RiskGate(limits)
    state = _state(equity=100_000.0, cash=100_000.0)
    intent = OrderIntent(symbol="AAPL", side="buy", notional=7_000.0, ref_price=150.0, pool="daily")
    decision = gate.evaluate(intent, state)
    assert decision.approved
    assert decision.approved_notional == pytest.approx(6_000.0, abs=1.0)


def test_gate_intraday_pool_uses_intraday_equity_fraction():
    """Intraday cap = max_position_pct * equity * intraday_pool_pct = 10% * 40k = $4,000."""
    limits = _limits()
    gate = RiskGate(limits)
    state = _state(equity=100_000.0, cash=100_000.0)
    intent = OrderIntent(symbol="AAPL", side="buy", notional=5_000.0, ref_price=150.0, pool="intraday")
    decision = gate.evaluate(intent, state)
    assert decision.approved
    assert decision.approved_notional == pytest.approx(4_000.0, abs=1.0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
venv/bin/python -m pytest tests/test_capital_pools.py -v
```
Expected: FAIL.

- [ ] **Step 3: Update `trader/config.py`**

In `RiskLimits`, remove `max_trades_per_day` field and add `intraday_pool_pct`:

```python
# Remove this line entirely:
#     max_trades_per_day: int = 5

# Add after max_position_pct:
    intraday_pool_pct: float = 0.40     # fraction of equity reserved for intraday (env: INTRADAY_POOL_PCT)
```

In `load_config()`, remove the `max_trades_per_day=...` line and add `intraday_pool_pct`:

```python
# Remove:
#     max_trades_per_day=int(os.getenv("RISK_MAX_TRADES_PER_DAY", "5")),

# Add inside RiskLimits(...):
            intraday_pool_pct=float(os.getenv("INTRADAY_POOL_PCT", "0.40")),
```

- [ ] **Step 4: Update `trader/risk/gate.py`**

**a) Add `intraday_deployed` to `AccountState`** (after `deployed_notional`):

```python
    deployed_notional: float = 0.0         # cumulative buy notional approved this tick (daily pool)
    intraday_deployed: float = 0.0         # cumulative buy notional approved this tick (intraday pool)
```

**b) Add `pool` to `OrderIntent`** (after `spread_pct`):

```python
    spread_pct: float = 0.0
    pool: str = "daily"                    # "daily" or "intraday" — determines capital pool cap
```

**c) Update check 6 in `RiskGate.evaluate`** — replace the single `cap = _cap_pct * state.equity` line with pool-aware sizing:

```python
        # 6. Max position size (buys only) — cap is a fraction of pool equity, not total equity.
        _cap_pct = limits.max_crypto_position_pct if _is_crypto else limits.max_position_pct
        _pool_fraction = (
            limits.intraday_pool_pct
            if intent.pool == "intraday"
            else (1.0 - limits.intraday_pool_pct)
        )
        cap = _cap_pct * state.equity * _pool_fraction
```

**d) Remove the commented-out `max_trades_per_day` block** (lines 170-174):

```python
        # Remove entirely:
        # DISABLED: max trades/day cap — monitoring uncapped performance.
        # if state.trades_today >= limits.max_trades_per_day:
        #     return RiskDecision.reject(
        #         f"max trades/day reached ({state.trades_today}/{limits.max_trades_per_day})"
        #     )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
venv/bin/python -m pytest tests/test_capital_pools.py -v
```
Expected: PASS (7 tests).

- [ ] **Step 6: Run full test suite**

```bash
venv/bin/python -m pytest --tb=short -q
```
Expected: all pass. If `test_risk_gate.py` or `test_config.py` fail due to `max_trades_per_day` references, update those tests to remove the field.

- [ ] **Step 7: Commit**

```bash
git add trader/config.py trader/risk/gate.py tests/test_capital_pools.py
git commit -m "feat(pools): 60/40 capital split — intraday_pool_pct, pool-aware gate sizing, remove max_trades_per_day"
```

---

## Task 3: DB migration + position_owners pool column + repo interface

**Files:**
- Create: `migrations/versions/002_position_owners_pool.py`
- Modify: `trader/portfolio/repository.py`
- Modify: `trader/portfolio/postgres_repo.py`
- Modify: `trader/portfolio/sqlite_repo.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `get_position_owners() -> dict[tuple[str, str], str]` — key is `(symbol, pool)`
  - `set_position_owner(symbol: str, strategy: str, pool: str = "daily") -> None`
  - `clear_position_owner(symbol: str, pool: str = "daily") -> None`

---

- [ ] **Step 1: Create Alembic migration**

```python
# migrations/versions/002_position_owners_pool.py
"""Add pool column to position_owners; change PK to (symbol, pool)

Revision ID: 002
Revises: 001
Create Date: 2026-06-28
"""
from __future__ import annotations
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE position_owners ADD COLUMN IF NOT EXISTS pool VARCHAR(10) NOT NULL DEFAULT 'daily';
        ALTER TABLE position_owners DROP CONSTRAINT IF EXISTS position_owners_pkey;
        ALTER TABLE position_owners ADD PRIMARY KEY (symbol, pool);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE position_owners DROP CONSTRAINT IF EXISTS position_owners_pkey;
        ALTER TABLE position_owners ADD PRIMARY KEY (symbol);
        ALTER TABLE position_owners DROP COLUMN IF EXISTS pool;
    """)
```

- [ ] **Step 2: Update abstract signatures in `trader/portfolio/repository.py`**

Replace the three `position_owner` method signatures:

```python
    @abstractmethod
    def get_position_owners(self) -> dict[tuple[str, str], str]:
        """Return all persisted ownership entries: (symbol, pool) -> strategy class name."""

    @abstractmethod
    def set_position_owner(self, symbol: str, strategy: str, pool: str = "daily") -> None:
        """Upsert ownership of (symbol, pool) to strategy. Called when a buy executes."""

    @abstractmethod
    def clear_position_owner(self, symbol: str, pool: str = "daily") -> None:
        """Remove ownership for (symbol, pool). Called when a sell executes."""
```

- [ ] **Step 3: Update `trader/portfolio/postgres_repo.py`**

Replace the three methods (lines 250-268):

```python
    def get_position_owners(self) -> dict[tuple[str, str], str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT symbol, pool, strategy FROM position_owners")
                return {(row["symbol"], row["pool"]): row["strategy"] for row in cur.fetchall()}

    def set_position_owner(self, symbol: str, strategy: str, pool: str = "daily") -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO position_owners (symbol, pool, strategy, updated_at) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (symbol, pool) DO UPDATE SET strategy=EXCLUDED.strategy, updated_at=EXCLUDED.updated_at",
                    (symbol, pool, strategy, _now()),
                )

    def clear_position_owner(self, symbol: str, pool: str = "daily") -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM position_owners WHERE symbol=%s AND pool=%s",
                    (symbol, pool),
                )
```

- [ ] **Step 4: Update `trader/portfolio/sqlite_repo.py`**

Replace the three methods (lines 260-275):

```python
    def get_position_owners(self) -> dict[tuple[str, str], str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT symbol, pool, strategy FROM position_owners").fetchall()
            return {(row["symbol"], row["pool"]): row["strategy"] for row in rows}

    def set_position_owner(self, symbol: str, strategy: str, pool: str = "daily") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO position_owners (symbol, pool, strategy, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(symbol, pool) DO UPDATE SET strategy=excluded.strategy, updated_at=excluded.updated_at",
                (symbol, pool, strategy, _now()),
            )

    def clear_position_owner(self, symbol: str, pool: str = "daily") -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM position_owners WHERE symbol=? AND pool=?",
                (symbol, pool),
            )
```

- [ ] **Step 5: Also update `_init_schema` in `postgres_repo.py`** to add the pool column idempotently at startup (before migrations run for existing installs):

In `_init_schema`, after the existing `ALTER TABLE` calls, add:

```python
                cur.execute("ALTER TABLE position_owners ADD COLUMN IF NOT EXISTS pool VARCHAR(10) NOT NULL DEFAULT 'daily'")
```

- [ ] **Step 6: Run tests**

```bash
venv/bin/python -m pytest tests/test_portfolio_repo.py -v
```
Expected: PASS. If `test_portfolio_repo.py` calls `set_position_owner` or `clear_position_owner` without `pool`, the default `"daily"` handles backward compat — no test changes needed.

- [ ] **Step 7: Run full suite**

```bash
venv/bin/python -m pytest --tb=short -q
```

- [ ] **Step 8: Commit**

```bash
git add migrations/versions/002_position_owners_pool.py \
        trader/portfolio/repository.py \
        trader/portfolio/postgres_repo.py \
        trader/portfolio/sqlite_repo.py
git commit -m "feat(db): position_owners pool column — composite PK (symbol, pool), repo interface updated"
```

---

## Task 4: Pipeline routing — intraday bars, EOD exit, pool-aware sizing

**Files:**
- Modify: `trader/pipeline.py`

**Interfaces:**
- Consumes from Task 1: `IntradayStrategy` from `trader.strategy.base`; `get_intraday_bars_batch` from `trader.data.alpaca_bars`
- Consumes from Task 2: `AccountState.intraday_deployed`; `OrderIntent.pool`
- Consumes from Task 3: `repo.get_position_owners()` returns `dict[tuple[str,str], str]`
- Produces: pipeline handles intraday strategies transparently; ownership key is `(symbol, pool)`

**Note:** This task has no new test file — changes are covered by the existing `tests/test_pipeline.py`. Run it after each sub-step.

---

- [ ] **Step 1: Add imports to `trader/pipeline.py`**

At the top of the file, add to existing imports:

```python
from trader.data.alpaca_bars import get_daily_bars, get_daily_bars_batch, get_live_prices_batch, get_intraday_bars_batch
```

Also add near the top (after `_BARS_LOOKBACK_DAYS`):

```python
_EOD_EXIT_MINUTES = int(os.getenv("EOD_EXIT_MINUTES", "15"))
```

And the import:

```python
import os
```

(if not already present — check: `os` is not currently imported in pipeline.py, it's needed for `os.getenv`).

- [ ] **Step 2: Update `run_pipeline` — intraday bar pre-fetch**

In `run_pipeline`, find the block that fetches `equity_symbols` (lines 124-138). Replace it with:

```python
    from trader.strategy.base import IntradayStrategy

    equity_symbols = list({
        s.symbol for s in strategies
        if not is_crypto_symbol(s.symbol) and not isinstance(s, IntradayStrategy)
    })
    live_prices: dict[str, float] = {}
    live_spread_pcts: dict[str, float] = {}
    if equity_symbols:
        end = asof
        start = end - timedelta(days=_BARS_LOOKBACK_DAYS)
        bars_cache: dict[str, object] = get_daily_bars_batch(equity_symbols, start, end, config)
        try:
            live_prices, live_spread_pcts = get_live_prices_batch(equity_symbols, config)
        except Exception:
            logger.warning("live quote fetch failed; stop-loss uses yesterday's close")
    else:
        end = asof
        start = end - timedelta(days=_BARS_LOOKBACK_DAYS)
        bars_cache = {}

    # Intraday bars — fetched per timeframe, no cache (small payload).
    intraday_caches: dict[str, dict[str, object]] = {}
    for _tf in ("1min", "5min"):
        _tf_syms = list({
            s.symbol for s in strategies
            if isinstance(s, IntradayStrategy) and s.bar_timeframe == _tf
        })
        if _tf_syms:
            intraday_caches[_tf] = get_intraday_bars_batch(_tf_syms, _tf, 390, config)

    # GapAndGo needs yesterday's close — reuse daily bars cache already fetched above.
    from trader.strategy.gap_and_go import GapAndGo
    for _s in strategies:
        if isinstance(_s, GapAndGo):
            _gap_sym = _s.symbol
            if _gap_sym not in bars_cache:
                _gap_daily = get_daily_bars_batch([_gap_sym], start, end, config)
                bars_cache.update(_gap_daily)
            if _gap_sym in bars_cache:
                _s.prev_close = float(bars_cache[_gap_sym]["close"].iloc[-1])

    # Also fetch live prices for intraday symbols.
    intraday_syms = list({
        s.symbol for s in strategies if isinstance(s, IntradayStrategy)
    })
    if intraday_syms:
        try:
            _iday_prices, _iday_spreads = get_live_prices_batch(intraday_syms, config)
            live_prices.update(_iday_prices)
            live_spread_pcts.update(_iday_spreads)
        except Exception:
            logger.warning("intraday live quote fetch failed")
```

- [ ] **Step 3: Pass `intraday_caches` to `_prepare_signal`**

In `run_pipeline`, update all calls to `_prepare_signal` to pass `intraday_caches`:

```python
        prep = _prepare_signal(
            config=config,
            strategy=strategy,
            repo=repo,
            state=state,
            asof=asof,
            bars_cache=bars_cache,
            intraday_caches=intraday_caches,
            live_prices=live_prices,
        )
```

- [ ] **Step 4: Update `_prepare_signal` signature and add intraday bar routing + EOD exit**

Replace the function signature and the bar-fetch block inside `_prepare_signal`:

```python
def _prepare_signal(
    *,
    config,
    strategy,
    repo,
    state,
    asof,
    bars_cache: dict | None = None,
    intraday_caches: dict | None = None,
    live_prices: dict | None = None,
):
```

Inside the `try` block, replace the `bars = _fetch_bars(...)` line with:

```python
        from trader.strategy.base import IntradayStrategy
        _is_intraday = isinstance(strategy, IntradayStrategy)
        _pool = "intraday" if _is_intraday else "daily"

        if _is_intraday:
            _tf = strategy.bar_timeframe
            bars = (intraday_caches or {}).get(_tf, {}).get(symbol)
            if bars is None or (hasattr(bars, "empty") and bars.empty):
                logger.warning("no intraday bar data for %s — skipping", symbol)
                return None
        else:
            import pandas as pd as _pd  # noqa — already imported above
            bars = _fetch_bars(symbol, start, end, config, cache=bars_cache)
```

Wait — `pd` is already imported via `import pandas as pd` inside the try block in the original. Let me fix that:

```python
        from trader.strategy.base import IntradayStrategy
        _is_intraday = isinstance(strategy, IntradayStrategy)
        _pool = "intraday" if _is_intraday else "daily"

        import pandas as pd
        end = asof
        start = end - timedelta(days=_BARS_LOOKBACK_DAYS)

        if _is_intraday:
            _tf = strategy.bar_timeframe
            bars = (intraday_caches or {}).get(_tf, {}).get(symbol)
            if bars is None or bars.empty:
                logger.warning("no intraday bar data for %s — skipping", symbol)
                return None
        else:
            bars = _fetch_bars(symbol, start, end, config, cache=bars_cache)
```

- [ ] **Step 5: Add EOD exit check inside `_prepare_signal`**

After the `warm_up` / cold-start block and before the stop-loss check, insert:

```python
        # EOD exit: force-sell intraday positions 15 min before market close.
        # Bypasses overlay and ownership conflict — same pattern as stop-loss.
        if _is_intraday and strategy.eod_exit and symbol in state.positions and state.positions[symbol] > 0:
            from zoneinfo import ZoneInfo as _ZI
            _ny = _ZI("America/New_York")
            _asof_ny = asof.astimezone(_ny) if asof.tzinfo else asof.replace(tzinfo=timezone.utc).astimezone(_ny)
            _close_ny = _asof_ny.replace(hour=16, minute=0, second=0, microsecond=0)
            if _asof_ny >= _close_ny - timedelta(minutes=_EOD_EXIT_MINUTES):
                signal = Signal(
                    symbol, "sell", 1.0,
                    f"eod-exit: intraday flat at {_asof_ny.strftime('%H:%M')} ET",
                )
```

- [ ] **Step 6: Update stop-loss exemption check to use pool key**

Replace line 332:

```python
        _stop_exempt = state.position_owners.get(symbol) == "DipRecovery"
```

With:

```python
        _stop_exempt = state.position_owners.get((symbol, "daily")) == "DipRecovery"
```

- [ ] **Step 7: Update ownership conflict check to use pool key**

Replace the ownership conflict block (around line 384):

```python
        if signal.side == "sell" and not signal.reason.startswith("stop-loss:") and not signal.reason.startswith("eod-exit:"):
            owner = state.position_owners.get((symbol, _pool))
            if owner is not None and owner != type(strategy).__name__:
                ...
```

- [ ] **Step 8: Skip fundamental gate and overlay for intraday strategies**

Replace the fundamental gate block:

```python
        is_first_entry = symbol not in state.positions or state.positions.get(symbol, 0.0) == 0.0
        if signal.side == "buy" and is_first_entry and not is_crypto_symbol(symbol) and not _is_intraday:
            date_str = asof.strftime("%Y-%m-%d")
            if not apply_fundamental_gate(symbol, bars, config, date_str):
                ...
```

Replace the overlay call:

```python
        if not signal.reason.startswith("stop-loss:") and not signal.reason.startswith("eod-exit:") and not _is_intraday:
            signal = apply_overlay(signal, bars, config)
```

- [ ] **Step 9: Update `_execute_signal` calls to pass `pool`**

In `run_pipeline`, every call to `_execute_signal` needs `pool=_pool`. However `_pool` is computed inside `_prepare_signal`. The cleanest fix: `_prepare_signal` returns `(signal, bars, run_id, pool)` for the non-terminal case. Update the function return and all callers:

Inside `_prepare_signal`, change the final return:

```python
        return signal, bars, run_id, _pool
```

In `run_pipeline`, unpack the extra value:

```python
        signal, bars, run_id, pool = prep
        if signal.side == "sell":
            result = _execute_signal(
                signal=signal, bars=bars, run_id=run_id, strategy=strategy,
                pool=pool, ...
            )
```

And for buys:

```python
            pending_buys.append((signal.strength, strategy, signal, bars, run_id, pool))
```

Then unpack in the Phase 2 loop:

```python
    for _, strategy, signal, bars, run_id, pool in pending_buys:
        ...
        result = _execute_signal(..., pool=pool, ...)
```

- [ ] **Step 10: Update `_execute_signal` signature and `OrderIntent` creation**

Add `pool: str = "daily"` parameter to `_execute_signal`:

```python
def _execute_signal(
    *,
    signal,
    bars,
    run_id: int,
    strategy,
    config,
    broker,
    repo,
    gate,
    kill_switch,
    state,
    asof,
    live_prices: dict | None = None,
    live_spread_pcts: dict | None = None,
    corr_factor: float = 1.0,
    pool: str = "daily",
):
```

Update the `OrderIntent` construction to pass pool:

```python
        intent = OrderIntent(
            symbol=symbol, side=signal.side,
            notional=notional, ref_price=ref_price, reason=signal.reason,
            spread_pct=spread_pct, pool=pool,
        )
```

- [ ] **Step 11: Update `_notional_for` for pool-aware free-cash**

Add `pool: str = "daily"` parameter to `_notional_for`:

```python
def _notional_for(signal, state, config, ref_price: float, bars=None, corr_factor: float = 1.0, pool: str = "daily") -> float:
```

Replace the `free_cash` computation:

```python
    if signal.side == "sell":
        held = state.positions.get(signal.symbol, 0.0)
        return max(held * ref_price, 1.0)
    is_crypto = is_crypto_symbol(signal.symbol)
    cap_pct = (
        config.risk.max_crypto_position_pct
        if is_crypto
        else config.risk.max_position_pct
    )
    if pool == "intraday":
        pool_cash = state.cash * config.risk.intraday_pool_pct
        free_cash = max(pool_cash - state.intraday_deployed, 0.0)
    else:
        pool_cash = state.cash * (1.0 - config.risk.intraday_pool_pct)
        free_cash = max(pool_cash - state.deployed_notional - config.risk.min_cash_reserve, 0.0)
    ...
```

Pass `pool` through the call in `_execute_signal`:

```python
        notional = _notional_for(signal, state, config, ref_price, bars=bars, corr_factor=corr_factor, pool=pool)
```

- [ ] **Step 12: Update `_advance_state` for pool-aware ownership and deployed tracking**

```python
def _advance_state(state, result, strategy, repo):
    from dataclasses import replace as _replace
    from trader.strategy.base import IntradayStrategy
    pool = "intraday" if isinstance(strategy, IntradayStrategy) else "daily"
    approved_notional = result.risk_decision.approved_notional or 0.0
    new_owners = dict(state.position_owners)
    owner_key = (result.symbol, pool)
    if result.signal is not None:
        if result.signal.side == "buy" and owner_key not in new_owners:
            new_owners[owner_key] = type(strategy).__name__
            try:
                repo.set_position_owner(result.symbol, type(strategy).__name__, pool)
            except Exception:
                logger.warning("failed to persist owner for %s/%s", result.symbol, pool)
        elif result.signal.side == "sell":
            new_owners.pop(owner_key, None)
            try:
                repo.clear_position_owner(result.symbol, pool)
            except Exception:
                logger.warning("failed to clear owner for %s/%s", result.symbol, pool)
    if pool == "intraday":
        new_intraday = state.intraday_deployed + (
            approved_notional if result.signal and result.signal.side == "buy" else 0.0
        )
        return _replace(
            state,
            trades_today=state.trades_today + 1,
            open_order_symbols=state.open_order_symbols | {result.symbol},
            position_owners=new_owners,
            intraday_deployed=new_intraday,
        )
    else:
        new_deployed = state.deployed_notional + (
            approved_notional if result.signal and result.signal.side == "buy" else 0.0
        )
        return _replace(
            state,
            trades_today=state.trades_today + 1,
            open_order_symbols=state.open_order_symbols | {result.symbol},
            position_owners=new_owners,
            deployed_notional=new_deployed,
        )
```

- [ ] **Step 13: Update `run_pipeline` — fix owner loading for composite key**

In `run_pipeline`, the owner-loading block uses `s in state.positions` (symbol string). With composite keys this needs adjustment:

```python
        loaded_owners = repo.get_position_owners()  # dict[tuple[str,str], str]
        loaded_owners = {
            key: o for key, o in loaded_owners.items()
            if key[0] in state.positions and o in active_strategy_names
        }
```

- [ ] **Step 14: Update `precompute_signals` to skip intraday**

```python
    for strategy in strategies:
        symbol = strategy.symbol
        from trader.strategy.base import IntradayStrategy
        if is_crypto_symbol(symbol) or isinstance(strategy, IntradayStrategy):
            continue
```

- [ ] **Step 15: Run full test suite**

```bash
venv/bin/python -m pytest --tb=short -q
```
Expected: all pass. Fix any import errors or type mismatches that surface.

- [ ] **Step 16: Commit**

```bash
git add trader/pipeline.py
git commit -m "feat(pipeline): intraday routing — bar pre-fetch, EOD exit, pool-aware sizing, composite ownership keys"
```

---

## Task 5: IntradayTrend strategy

**Files:**
- Create: `trader/strategy/intraday_trend.py`
- Create: `tests/test_intraday_trend.py`

**Interfaces:**
- Consumes from Task 1: `IntradayStrategy` from `trader.strategy.base`
- Consumes: `supertrend`, `adx` from `trader.strategy.indicators`
- Produces: `IntradayTrend(symbol, atr_n=14, multiplier=3.0, adx_threshold=20.0)` — `bar_timeframe="5min"`

---

- [ ] **Step 1: Write failing tests**

```python
# tests/test_intraday_trend.py
from __future__ import annotations
import numpy as np
import pandas as pd
import pytest
from trader.strategy.base import IntradayStrategy
from trader.strategy.intraday_trend import IntradayTrend


def _make_intraday_bars(closes, n_per_day=80) -> pd.DataFrame:
    """Minute-level OHLCV bars — mimics intraday bar format."""
    n = len(closes)
    timestamps = pd.date_range("2024-01-15 09:30", periods=n, freq="5min")
    c = pd.Series(closes, index=timestamps, dtype=float)
    return pd.DataFrame({
        "open": c.shift(1).fillna(c.iloc[0]),
        "high": c + 0.5,
        "low": c - 0.5,
        "close": c,
        "volume": 500_000,
    }, index=timestamps)


def _uptrend(n=80):
    return _make_intraday_bars([100.0 + i * 0.6 for i in range(n)])


def _downtrend(n=80):
    return _make_intraday_bars([150.0 - i * 0.6 for i in range(n)])


def test_is_intraday_strategy():
    assert isinstance(IntradayTrend("AAPL"), IntradayStrategy)


def test_bar_timeframe_is_5min():
    assert IntradayTrend("AAPL").bar_timeframe == "5min"


def test_buy_in_uptrend():
    bars = _uptrend()
    sig = IntradayTrend("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "buy"
    assert 0.0 < sig.strength <= 1.0


def test_sell_in_downtrend():
    bars = _downtrend()
    sig = IntradayTrend("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "sell"


def test_hold_on_insufficient_history():
    bars = _make_intraday_bars([100.0 + i for i in range(5)])
    sig = IntradayTrend("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_hold_in_choppy_market():
    n = 80
    closes = [100.0 + (0.05 if i % 2 == 0 else -0.05) for i in range(n)]
    bars = _make_intraday_bars(closes)
    sig = IntradayTrend("AAPL", adx_threshold=20.0).generate(bars, bars.index[-1])
    assert sig.side == "hold"
```

- [ ] **Step 2: Run to verify they fail**

```bash
venv/bin/python -m pytest tests/test_intraday_trend.py -v
```
Expected: FAIL — `IntradayTrend` not defined.

- [ ] **Step 3: Create `trader/strategy/intraday_trend.py`**

```python
"""IntradayTrend — SuperTrend on 5-min bars. Logic identical to SuperTrend daily strategy."""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import IntradayStrategy, Signal
from trader.strategy.indicators import adx, supertrend


class IntradayTrend(IntradayStrategy):
    """SuperTrend trend-following on 5-min intraday bars with ADX regime filter."""

    bar_timeframe = "5min"

    def __init__(
        self,
        symbol: str,
        atr_n: int = 14,
        multiplier: float = 3.0,
        adx_threshold: float = 20.0,
    ) -> None:
        super().__init__(symbol)
        self.atr_n = atr_n
        self.multiplier = multiplier
        self.adx_threshold = adx_threshold

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        min_bars = self.atr_n * 2 + 1
        if len(bars) < min_bars:
            return Signal(self.symbol, "hold", 0.0, "insufficient history for IntradayTrend")

        high, low, close = bars["high"], bars["low"], bars["close"]
        st_line, direction = supertrend(high, low, close, self.atr_n, self.multiplier)
        adx_val = adx(high, low, close, self.atr_n)

        curr_st = float(st_line.iloc[-1])
        curr_dir = float(direction.iloc[-1])
        curr_adx = float(adx_val.iloc[-1])
        curr_close = float(close.iloc[-1])

        if pd.isna(curr_st) or pd.isna(curr_dir) or pd.isna(curr_adx):
            return Signal(self.symbol, "hold", 0.0, "SuperTrend/ADX not yet defined")

        if curr_dir == 1.0:
            if curr_adx < self.adx_threshold:
                return Signal(
                    self.symbol, "hold", 0.0,
                    f"uptrend but ADX {curr_adx:.1f} < {self.adx_threshold} — choppy",
                )
            spread = (curr_close - curr_st) / curr_st if curr_st != 0.0 else 0.0
            return Signal(
                self.symbol, "buy", float(min(abs(spread) * 10.0, 1.0)),
                f"ST {curr_st:.2f} < close {curr_close:.2f}, ADX {curr_adx:.1f}",
            )

        spread = (curr_st - curr_close) / curr_st if curr_st != 0.0 else 0.0
        return Signal(
            self.symbol, "sell", float(min(abs(spread) * 10.0, 1.0)),
            f"ST {curr_st:.2f} > close {curr_close:.2f}, ADX {curr_adx:.1f}",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
venv/bin/python -m pytest tests/test_intraday_trend.py -v
```
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add trader/strategy/intraday_trend.py tests/test_intraday_trend.py
git commit -m "feat(strategy): IntradayTrend — SuperTrend on 5-min intraday bars"
```

---

## Task 6: VWAPReversion strategy

**Files:**
- Create: `trader/strategy/vwap_reversion.py`
- Create: `tests/test_vwap_reversion.py`

**Interfaces:**
- Consumes from Task 1: `IntradayStrategy`
- Produces: `VWAPReversion(symbol)` — `bar_timeframe="1min"`, buys when price > 2σ below VWAP, sells at VWAP

---

- [ ] **Step 1: Write failing tests**

```python
# tests/test_vwap_reversion.py
from __future__ import annotations
import numpy as np
import pandas as pd
import pytest
from trader.strategy.base import IntradayStrategy
from trader.strategy.vwap_reversion import VWAPReversion


def _make_bars(closes, volumes=None) -> pd.DataFrame:
    n = len(closes)
    ts = pd.date_range("2024-01-15 09:30", periods=n, freq="1min")
    c = pd.Series(closes, index=ts, dtype=float)
    v = pd.Series(volumes or [1_000_000] * n, index=ts, dtype=float)
    return pd.DataFrame({
        "open": c.shift(1).fillna(c.iloc[0]),
        "high": c + 0.20,
        "low": c - 0.20,
        "close": c,
        "volume": v,
    }, index=ts)


def _vwap_bars_with_dip(n=60, vwap_close=100.0, dip_sigma=2.5):
    """Bars where VWAP ≈ vwap_close and last bar dips dip_sigma below VWAP std."""
    closes = [vwap_close] * (n - 1)
    # Build bars up to n-1 bars; compute approx std, then set last bar to dip
    df_pre = _make_bars(closes)
    vwap = (df_pre["close"] * df_pre["volume"]).cumsum() / df_pre["volume"].cumsum()
    dev = df_pre["close"] - vwap
    std_val = float(dev.rolling(20).std().iloc[-1]) or 0.5
    dip_close = vwap_close - dip_sigma * std_val - 0.01
    closes.append(dip_close)
    return _make_bars(closes)


def test_is_intraday_strategy():
    assert isinstance(VWAPReversion("AAPL"), IntradayStrategy)


def test_bar_timeframe_is_1min():
    assert VWAPReversion("AAPL").bar_timeframe == "1min"


def test_hold_on_insufficient_bars():
    bars = _make_bars([100.0 + i for i in range(10)])
    sig = VWAPReversion("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_buy_when_below_vwap_2sigma():
    bars = _vwap_bars_with_dip(n=60, dip_sigma=2.5)
    sig = VWAPReversion("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "buy"
    assert sig.strength > 0.0


def test_hold_when_near_vwap():
    closes = [100.0 + np.sin(i / 5) * 0.1 for i in range(60)]
    bars = _make_bars(closes)
    sig = VWAPReversion("AAPL").generate(bars, bars.index[-1])
    assert sig.side in {"hold", "buy"}  # should not sell when near VWAP


def test_sell_when_at_or_above_vwap_with_position():
    """Strategy emits sell when price returns to VWAP (state tracked via _entered)."""
    strat = VWAPReversion("AAPL")
    # Simulate entered state
    strat._entered = True
    closes = [100.0] * 60  # price == vwap (all same close, volume uniform → vwap == close)
    bars = _make_bars(closes)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "sell"


def test_strength_scales_with_deviation():
    bars_2 = _vwap_bars_with_dip(n=60, dip_sigma=2.5)
    bars_3 = _vwap_bars_with_dip(n=60, dip_sigma=3.5)
    sig_2 = VWAPReversion("AAPL").generate(bars_2, bars_2.index[-1])
    sig_3 = VWAPReversion("AAPL").generate(bars_3, bars_3.index[-1])
    if sig_2.side == "buy" and sig_3.side == "buy":
        assert sig_3.strength >= sig_2.strength
```

- [ ] **Step 2: Run to verify they fail**

```bash
venv/bin/python -m pytest tests/test_vwap_reversion.py -v
```

- [ ] **Step 3: Create `trader/strategy/vwap_reversion.py`**

```python
"""VWAPReversion — fade >2σ deviations from intraday VWAP on 1-min bars."""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import IntradayStrategy, Signal


class VWAPReversion(IntradayStrategy):
    """Buy when price is >2σ below VWAP; sell when price returns to VWAP or above.

    VWAP resets each call (bars are today-only). Requires 20-bar warm-up.
    _entered tracks whether we are in a position opened by this strategy instance.
    warm_up() reconstructs _entered from bar history on cold start.
    """

    bar_timeframe = "1min"

    def __init__(self, symbol: str, sigma_entry: float = 2.0, std_window: int = 20) -> None:
        super().__init__(symbol)
        self.sigma_entry = sigma_entry
        self.std_window = std_window
        self._entered = False

    def warm_up(self, bars: pd.DataFrame) -> None:
        self._entered = True
        self._warmed_up = True

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        if len(bars) < self.std_window:
            return Signal(self.symbol, "hold", 0.0, f"warm-up: need {self.std_window} bars")

        vwap = (bars["close"] * bars["volume"]).cumsum() / bars["volume"].cumsum()
        deviation = bars["close"] - vwap
        std = deviation.rolling(self.std_window).std()

        curr_close = float(bars["close"].iloc[-1])
        curr_vwap = float(vwap.iloc[-1])
        curr_std = float(std.iloc[-1])

        if pd.isna(curr_std) or curr_std == 0:
            return Signal(self.symbol, "hold", 0.0, "std undefined")

        if self._entered:
            if curr_close >= curr_vwap:
                self._entered = False
                return Signal(
                    self.symbol, "sell", 1.0,
                    f"VWAP reversion complete: close {curr_close:.2f} >= vwap {curr_vwap:.2f}",
                )
            return Signal(self.symbol, "hold", 0.0, "holding — waiting for VWAP reversion")

        sigma_distance = (curr_vwap - curr_close) / curr_std
        if sigma_distance >= self.sigma_entry:
            self._entered = True
            strength = float(min(sigma_distance / 3.0, 1.0))
            return Signal(
                self.symbol, "buy", strength,
                f"VWAP reversion entry: {sigma_distance:.1f}σ below VWAP {curr_vwap:.2f}",
            )

        return Signal(self.symbol, "hold", 0.0, f"deviation {sigma_distance:.2f}σ < {self.sigma_entry}σ threshold")
```

- [ ] **Step 4: Run tests**

```bash
venv/bin/python -m pytest tests/test_vwap_reversion.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/strategy/vwap_reversion.py tests/test_vwap_reversion.py
git commit -m "feat(strategy): VWAPReversion — 1-min VWAP mean reversion with 2σ entry"
```

---

## Task 7: GapAndGo strategy

**Files:**
- Create: `trader/strategy/gap_and_go.py`
- Create: `tests/test_gap_and_go.py`

**Interfaces:**
- Consumes from Task 1: `IntradayStrategy`
- Produces: `GapAndGo(symbol)` — `bar_timeframe="1min"`; `prev_close` attribute injected by pipeline before each `generate()` call; entry window bars 5–9 (9:35–9:39 AM)

---

- [ ] **Step 1: Write failing tests**

```python
# tests/test_gap_and_go.py
from __future__ import annotations
import pandas as pd
import pytest
from trader.strategy.base import IntradayStrategy
from trader.strategy.gap_and_go import GapAndGo


def _make_bars(opens, closes, volumes=None, start="2024-01-15 09:30") -> pd.DataFrame:
    n = len(closes)
    ts = pd.date_range(start, periods=n, freq="1min")
    v = pd.Series(volumes or [2_000_000] * n, index=ts, dtype=float)
    c = pd.Series(closes, index=ts, dtype=float)
    o = pd.Series(opens, index=ts, dtype=float)
    return pd.DataFrame({
        "open": o,
        "high": c + 0.5,
        "low": c - 0.5,
        "close": c,
        "volume": v,
    }, index=ts)


def test_is_intraday_strategy():
    assert isinstance(GapAndGo("AAPL"), IntradayStrategy)


def test_bar_timeframe_is_1min():
    assert GapAndGo("AAPL").bar_timeframe == "1min"


def test_hold_before_entry_window():
    """Bars 0-4 (9:30-9:34): no entry yet."""
    strat = GapAndGo("AAPL")
    strat.prev_close = 100.0
    opens = [103.0] * 5  # gap up 3%
    closes = [103.5] * 5
    bars = _make_bars(opens, closes, volumes=[3_000_000] * 5)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_buy_on_valid_gap_in_entry_window():
    """Bar 5 (9:35): gap>2%, volume>1.5x avg, price > prev_close → buy."""
    strat = GapAndGo("AAPL")
    strat.prev_close = 100.0
    avg_vol = 1_000_000
    # bars 0-4: normal volume; bar 5: high volume gap
    volumes = [avg_vol] * 5 + [avg_vol * 2]
    opens = [103.0] * 6
    closes = [103.5] * 6
    bars = _make_bars(opens, closes, volumes=volumes)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "buy"


def test_no_entry_after_window_close():
    """Bars 0-9 pass by; bar 10 is after window — no entry allowed."""
    strat = GapAndGo("AAPL")
    strat.prev_close = 100.0
    volumes = [2_000_000] * 10
    opens = [103.0] * 10
    closes = [103.5] * 10
    bars = _make_bars(opens, closes, volumes=volumes)
    sig = strat.generate(bars, bars.index[-1])
    # window closed — hold or no entry
    assert sig.side in {"hold", "sell"}


def test_hold_when_gap_insufficient():
    """Gap < 2% → no entry."""
    strat = GapAndGo("AAPL")
    strat.prev_close = 100.0
    volumes = [2_000_000] * 6
    opens = [101.0] * 6  # only 1% gap
    closes = [101.5] * 6
    bars = _make_bars(opens, closes, volumes=volumes)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_sell_when_momentum_fades():
    """After entry, close < entry_bar_open → sell."""
    strat = GapAndGo("AAPL")
    strat.prev_close = 100.0
    strat._entered = True
    strat._entry_bar_open = 103.0
    volumes = [2_000_000] * 6
    opens = [103.0] * 6
    closes = [103.5] * 5 + [102.5]  # last bar: close < entry_bar_open
    bars = _make_bars(opens, closes, volumes=volumes)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "sell"
```

- [ ] **Step 2: Run to verify they fail**

```bash
venv/bin/python -m pytest tests/test_gap_and_go.py -v
```

- [ ] **Step 3: Create `trader/strategy/gap_and_go.py`**

```python
"""GapAndGo — pre-market gap momentum strategy on 1-min bars.

Entry: bar 5-9 only (9:35-9:39 AM ET). prev_close injected by pipeline from daily bars cache.
Exit: close < entry_bar_open (momentum faded) or EOD exit from pipeline.
"""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import IntradayStrategy, Signal

_ENTRY_BAR_START = 5   # bar index 5 = 9:35 AM (0-indexed from 9:30)
_ENTRY_BAR_END = 9     # bar index 9 = 9:39 AM (inclusive)
_GAP_MIN_PCT = 0.02    # 2% gap minimum
_VOLUME_MULTIPLIER = 1.5


class GapAndGo(IntradayStrategy):
    """Enter long on gap-up days when gap holds and volume confirms at 9:35 AM."""

    bar_timeframe = "1min"

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self.prev_close: float = 0.0
        self._entered = False
        self._entry_bar_open: float = 0.0
        self._entry_attempted = False

    def warm_up(self, bars: pd.DataFrame) -> None:
        self._entered = True
        self._warmed_up = True

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        if self.prev_close <= 0.0:
            return Signal(self.symbol, "hold", 0.0, "prev_close not set")

        curr_idx = len(bars) - 1
        curr_close = float(bars["close"].iloc[-1])

        if self._entered:
            if curr_close < self._entry_bar_open:
                self._entered = False
                return Signal(
                    self.symbol, "sell", 1.0,
                    f"gap momentum faded: close {curr_close:.2f} < entry open {self._entry_bar_open:.2f}",
                )
            return Signal(self.symbol, "hold", 0.0, "gap trade active — holding")

        # Window closed or already attempted — no new entries.
        if self._entry_attempted or curr_idx > _ENTRY_BAR_END:
            return Signal(self.symbol, "hold", 0.0, "entry window closed")

        if curr_idx < _ENTRY_BAR_START:
            return Signal(self.symbol, "hold", 0.0, f"waiting for entry window (bar {curr_idx})")

        # Entry window: bars 5-9.
        first_open = float(bars["open"].iloc[0])
        gap_pct = (first_open - self.prev_close) / self.prev_close

        if gap_pct < _GAP_MIN_PCT:
            self._entry_attempted = True
            return Signal(
                self.symbol, "hold", 0.0,
                f"gap {gap_pct:.2%} < {_GAP_MIN_PCT:.0%} minimum",
            )

        avg_volume = float(bars["volume"].mean())
        entry_volume = float(bars["volume"].iloc[curr_idx])
        if entry_volume < avg_volume * _VOLUME_MULTIPLIER:
            return Signal(
                self.symbol, "hold", 0.0,
                f"entry volume {entry_volume:,.0f} < {_VOLUME_MULTIPLIER}x avg {avg_volume:,.0f}",
            )

        if curr_close <= self.prev_close:
            return Signal(
                self.symbol, "hold", 0.0,
                f"price {curr_close:.2f} not holding above prev_close {self.prev_close:.2f}",
            )

        self._entered = True
        self._entry_attempted = True
        self._entry_bar_open = float(bars["open"].iloc[curr_idx])
        return Signal(
            self.symbol, "buy", min(gap_pct / 0.05, 1.0),
            f"gap {gap_pct:.2%} confirmed at bar {curr_idx}, vol {entry_volume:,.0f}",
        )
```

- [ ] **Step 4: Run tests**

```bash
venv/bin/python -m pytest tests/test_gap_and_go.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/strategy/gap_and_go.py tests/test_gap_and_go.py
git commit -m "feat(strategy): GapAndGo — 1-min gap momentum, entry window 9:35-9:39 AM"
```

---

## Task 8: OpeningRangeBreakout strategy

**Files:**
- Create: `trader/strategy/orb.py`
- Create: `tests/test_orb.py`

**Interfaces:**
- Consumes from Task 1: `IntradayStrategy`
- Produces: `OpeningRangeBreakout(symbol)` — `bar_timeframe="1min"`; range set from bars 0-29 (9:30-9:59); buy on first close above ORH with volume confirmation

---

- [ ] **Step 1: Write failing tests**

```python
# tests/test_orb.py
from __future__ import annotations
import pandas as pd
import pytest
from trader.strategy.base import IntradayStrategy
from trader.strategy.orb import OpeningRangeBreakout

_RANGE_BARS = 30


def _make_bars(closes, highs=None, lows=None, volumes=None) -> pd.DataFrame:
    n = len(closes)
    ts = pd.date_range("2024-01-15 09:30", periods=n, freq="1min")
    c = pd.Series(closes, index=ts, dtype=float)
    h = pd.Series(highs, index=ts, dtype=float) if highs else c + 0.5
    lo = pd.Series(lows, index=ts, dtype=float) if lows else c - 0.5
    v = pd.Series(volumes or [1_000_000] * n, index=ts, dtype=float)
    return pd.DataFrame({"open": c, "high": h, "low": lo, "close": c, "volume": v}, index=ts)


def _range_bars_with_breakout(range_high=105.0, range_low=95.0, breakout_close=106.0):
    """30 range bars + 1 breakout bar with high volume."""
    closes = list(range(95, 95 + _RANGE_BARS))  # range 95-124... no
    # simpler: flat range bars
    closes = [100.0] * (_RANGE_BARS - 1) + [breakout_close]
    highs = [range_high] * (_RANGE_BARS - 1) + [breakout_close + 0.5]
    lows = [range_low] * (_RANGE_BARS - 1) + [breakout_close - 0.5]
    volumes = [500_000] * (_RANGE_BARS - 1) + [1_500_000]  # last bar: high vol
    return _make_bars(closes, highs, lows, volumes)


def test_is_intraday_strategy():
    assert isinstance(OpeningRangeBreakout("AAPL"), IntradayStrategy)


def test_bar_timeframe_is_1min():
    assert OpeningRangeBreakout("AAPL").bar_timeframe == "1min"


def test_hold_before_range_set():
    bars = _make_bars([100.0] * 10)
    sig = OpeningRangeBreakout("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_buy_on_breakout_above_orh():
    bars = _range_bars_with_breakout(range_high=105.0, breakout_close=106.0)
    sig = OpeningRangeBreakout("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "buy"
    assert sig.strength > 0.0


def test_no_entry_inside_range():
    """Close inside range after range is set → hold."""
    closes = [100.0] * _RANGE_BARS + [102.0]  # 102 < range_high of 100.5 is inside
    highs = [100.5] * _RANGE_BARS + [102.5]
    lows = [99.5] * _RANGE_BARS + [101.5]
    # ORH = max(high[0:30]) = 100.5; close 102 > 100.5 → actually a breakout
    # Let's keep close below ORH
    closes = [100.0] * _RANGE_BARS + [100.3]
    highs = [100.5] * _RANGE_BARS + [100.8]
    lows = [99.5] * _RANGE_BARS + [100.0]
    bars = _make_bars(closes, highs, lows)
    sig = OpeningRangeBreakout("AAPL").generate(bars, bars.index[-1])
    assert sig.side == "hold"


def test_sell_when_close_drops_below_orl():
    strat = OpeningRangeBreakout("AAPL")
    strat._range_set = True
    strat._orh = 105.0
    strat._orl = 95.0
    strat._entered = True
    closes = [100.0] * _RANGE_BARS + [94.0]  # below ORL
    bars = _make_bars(closes)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "sell"


def test_no_reentry_after_exit():
    """After exiting, no new buy even on another breakout."""
    strat = OpeningRangeBreakout("AAPL")
    strat._range_set = True
    strat._orh = 105.0
    strat._orl = 95.0
    strat._entered = False
    strat._exited = True
    bars = _range_bars_with_breakout(range_high=105.0, breakout_close=106.0)
    sig = strat.generate(bars, bars.index[-1])
    assert sig.side == "hold"
```

- [ ] **Step 2: Run to verify they fail**

```bash
venv/bin/python -m pytest tests/test_orb.py -v
```

- [ ] **Step 3: Create `trader/strategy/orb.py`**

```python
"""OpeningRangeBreakout — buy close above ORH with volume; sell below ORL or EOD."""
from __future__ import annotations

import pandas as pd

from trader.strategy.base import IntradayStrategy, Signal

_RANGE_BARS = 30          # bars 0-29 = 9:30-9:59 AM
_VOLUME_MULTIPLIER = 1.5


class OpeningRangeBreakout(IntradayStrategy):
    """Enter long on first close above opening range high (ORH) with volume confirmation.

    Range forms during bars 0-29 (first 30 minutes). No re-entry after exit.
    """

    bar_timeframe = "1min"

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self._orh: float = 0.0
        self._orl: float = 0.0
        self._range_set: bool = False
        self._entered: bool = False
        self._exited: bool = False

    def warm_up(self, bars: pd.DataFrame) -> None:
        self._entered = True
        self._warmed_up = True

    def _decide(self, bars: pd.DataFrame, asof: pd.Timestamp) -> Signal:
        curr_idx = len(bars) - 1
        curr_close = float(bars["close"].iloc[-1])

        if not self._range_set:
            if curr_idx < _RANGE_BARS - 1:
                return Signal(
                    self.symbol, "hold", 0.0,
                    f"forming opening range (bar {curr_idx}/{_RANGE_BARS - 1})",
                )
            range_bars = bars.iloc[:_RANGE_BARS]
            self._orh = float(range_bars["high"].max())
            self._orl = float(range_bars["low"].min())
            self._range_set = True

        if self._entered:
            if curr_close < self._orl:
                self._entered = False
                self._exited = True
                return Signal(
                    self.symbol, "sell", 1.0,
                    f"ORB violated: close {curr_close:.2f} < ORL {self._orl:.2f}",
                )
            return Signal(self.symbol, "hold", 0.0, "ORB trade active — holding")

        if self._exited:
            return Signal(self.symbol, "hold", 0.0, "no re-entry after ORB exit")

        if curr_close <= self._orh:
            return Signal(
                self.symbol, "hold", 0.0,
                f"no breakout: close {curr_close:.2f} <= ORH {self._orh:.2f}",
            )

        avg_volume = float(bars["volume"].mean())
        entry_volume = float(bars["volume"].iloc[-1])
        if entry_volume < avg_volume * _VOLUME_MULTIPLIER:
            return Signal(
                self.symbol, "hold", 0.0,
                f"breakout volume {entry_volume:,.0f} < {_VOLUME_MULTIPLIER}x avg",
            )

        self._entered = True
        strength = float(min((curr_close - self._orh) / self._orh, 1.0))
        return Signal(
            self.symbol, "buy", max(strength, 0.01),
            f"ORB breakout: close {curr_close:.2f} > ORH {self._orh:.2f}",
        )
```

- [ ] **Step 4: Run tests**

```bash
venv/bin/python -m pytest tests/test_orb.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/strategy/orb.py tests/test_orb.py
git commit -m "feat(strategy): OpeningRangeBreakout — 1-min ORB, first 30-min range, volume-confirmed breakout"
```

---

## Task 9: Scheduler registration

**Files:**
- Modify: `trader/scheduler.py`

**Interfaces:**
- Consumes from Tasks 5-8: all four intraday strategy classes
- Produces: intraday strategies built and passed to `run_pipeline` alongside daily strategies; `INTRADAY_ALLOWLIST` env var controls symbols

---

- [ ] **Step 1: Read the start of `_scheduler_loop` / `start_scheduler` to understand the strategy list assembly**

Open `trader/scheduler.py` and locate `_build_strategies_for` (line 250) and the code that calls `run_pipeline`. The intraday strategies will be appended to the same `strategies` list passed to `run_pipeline`.

- [ ] **Step 2: Add `_build_intraday_strategies_for` function**

After `_build_strategies_for`, add:

```python
def _build_intraday_strategies_for(config: Config, symbols: "list[str]") -> "list[Strategy]":
    """Build all 4 intraday strategies per symbol.

    Uses INTRADAY_ALLOWLIST env var; falls back to the same symbols as the equity stack.
    """
    from trader.strategy.intraday_trend import IntradayTrend
    from trader.strategy.vwap_reversion import VWAPReversion
    from trader.strategy.gap_and_go import GapAndGo
    from trader.strategy.orb import OpeningRangeBreakout

    strategies: list[Strategy] = []
    for sym in symbols:
        strategies.append(IntradayTrend(symbol=sym))
        strategies.append(VWAPReversion(symbol=sym))
        strategies.append(GapAndGo(symbol=sym))
        strategies.append(OpeningRangeBreakout(symbol=sym))
    return strategies
```

- [ ] **Step 3: Load `INTRADAY_ALLOWLIST` and register intraday strategies**

Find where `current_strategies` is built from `_build_strategies_for` in the main scheduler loop (look for calls to `_build_strategies_for`). After that call, append intraday strategies:

```python
# Near top of start_scheduler or _scheduler_loop, where current_strategies is built:
_raw_intraday = os.getenv("INTRADAY_ALLOWLIST", "").strip()
_intraday_symbols = (
    [s.strip().upper() for s in _raw_intraday.split(",") if s.strip()]
    if _raw_intraday
    else list(equity_symbols)  # falls back to same universe as daily
)

intraday_strategies = _build_intraday_strategies_for(config, _intraday_symbols)
current_strategies = daily_strategies + intraday_strategies
```

The exact insertion point depends on where `current_strategies` is assembled. The pattern to follow: find the line `current_strategies = _build_strategies_for(config, symbols)` and extend it.

- [ ] **Step 4: Run existing scheduler tests**

```bash
venv/bin/python -m pytest tests/test_scheduler.py -v
```
Expected: PASS. The intraday strategies are additive — no existing scheduler behavior changes.

- [ ] **Step 5: Run full test suite**

```bash
venv/bin/python -m pytest --tb=short -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add trader/scheduler.py
git commit -m "feat(scheduler): register intraday strategy stack — IntradayTrend, VWAPReversion, GapAndGo, ORB via INTRADAY_ALLOWLIST"
```

---

## Self-Review Checklist

After implementing all tasks, verify:

- [ ] `isinstance(s, IntradayStrategy)` gates all intraday-specific behaviour — no scattered `if bar_timeframe` checks
- [ ] `position_owners` key is `(symbol, pool)` everywhere — grep: `position_owners.get(symbol)` should return 0 results
- [ ] `max_trades_per_day` absent from codebase — grep: `max_trades_per_day` should return 0 results
- [ ] `AccountState.intraday_deployed` tracked separately from `deployed_notional`
- [ ] EOD exit reason starts with `"eod-exit:"` — bypasses overlay and ownership conflict
- [ ] `precompute_signals` skips `IntradayStrategy` instances
- [ ] `get_intraday_bars_batch` does NOT normalize index to daily dates
- [ ] GapAndGo's `prev_close` is injected by pipeline (not fetched inside `_decide`)
- [ ] Migration `002` chains `down_revision = "001"`
- [ ] `sqlite_repo.py` conflict target is `(symbol, pool)` not `(symbol)`
