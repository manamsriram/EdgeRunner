# Performance Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a live paper trading performance tracker that pulls from Alpaca + Supabase, computes Sharpe/drawdown/win-rate/profit-factor against hard thresholds, and surfaces a PASS/FAIL go-live verdict via CLI, API, and React dashboard.

**Architecture:** New `trader/performance/` package handles all metric computation (injected broker + repo, unit-testable without network). A FastAPI route wraps it with a 5-min cache and async execution. A CLI script uses the same compute function. A new React page displays metric tiles, verdict banner, and benchmark comparison.

**Tech Stack:** Python 3.12+, FastAPI, alpaca-py, psycopg2, pandas, numpy, React 18, react-query, recharts, Tailwind CSS

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `trader/execution/broker.py` | Modify | Add `period` param to `get_portfolio_history()`; add `get_account_activities()` |
| `trader/portfolio/repository.py` | Modify | Add `get_strategy_signal_counts()` abstract method |
| `trader/portfolio/sqlite_repo.py` | Modify | Implement `get_strategy_signal_counts()` |
| `trader/portfolio/postgres_repo.py` | Modify | Implement `get_strategy_signal_counts()` |
| `trader/performance/__init__.py` | Create | Package marker |
| `trader/performance/metrics.py` | Create | `LiveMetrics` + `compute_live_metrics()` |
| `tests/test_performance_metrics.py` | Create | Unit tests (TDD — written before metrics.py is complete) |
| `api/routes/performance.py` | Create | `GET /api/performance` with cache + run_in_executor |
| `api/main.py` | Modify | Register performance router |
| `scripts/performance_tracker.py` | Create | CLI script |
| `frontend/src/lib/api.ts` | Modify | Add `getPerformance()` + `PerformanceMetrics` type |
| `frontend/src/pages/Performance.tsx` | Create | Dashboard page |
| `frontend/src/components/ProtectedLayout.tsx` | Modify | Add Performance to NAV |
| `frontend/src/App.tsx` | Modify | Add `/performance` route |

---

## Task 1: Extend broker.py — portfolio history period + account activities

**Files:**
- Modify: `trader/execution/broker.py`

- [ ] **Step 1: Update `_TradingClient` Protocol to add `get_account_activities`**

In `trader/execution/broker.py`, replace the existing Protocol `get_portfolio_history` signature and add `get_account_activities`:

```python
class _TradingClient(Protocol):
    def get_account(self) -> Any: ...
    def get_all_positions(self) -> list[Any]: ...
    def get_orders(self, filter: Any) -> list[Any]: ...  # noqa: A002 - alpaca's kw name
    def submit_order(self, order_data: Any) -> Any: ...
    def get_order_by_client_id(self, client_id: str) -> Any: ...
    def get_portfolio_history(self, filter: Any = None) -> Any: ...
    def get_account_activities(self, filter: Any = None) -> Any: ...
```

- [ ] **Step 2: Add `period` parameter to `get_portfolio_history()`**

Replace the existing `get_portfolio_history` method body:

```python
def get_portfolio_history(self, period: str = "1A") -> dict | None:
    """Return {"timestamp": [...ISO strings...], "equity": [...floats...]} or None.

    `period` follows Alpaca's convention: "1D", "1W", "1M", "3M", "6M", "1A".
    Defaults to "1A" so callers get a full year of daily equity data for Sharpe
    and drawdown computation. The existing /api/portfolio/history endpoint uses
    the default and is unaffected.
    """
    try:
        client = self._ensure_client()
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        request = GetPortfolioHistoryRequest(period=period, timeframe="1D")
        history = client.get_portfolio_history(filter=request)

        def _ts_to_iso(t: Any) -> str:
            if hasattr(t, "isoformat"):
                return t.isoformat()
            if isinstance(t, (int, float)):
                return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
            return str(t)

        pairs = [
            (t, e)
            for t, e in zip(history.timestamp, history.equity)
            if e is not None
        ]
        if not pairs:
            return None
        timestamps, equities = zip(*pairs)
        return {
            "timestamp": [_ts_to_iso(t) for t in timestamps],
            "equity": list(equities),
        }
    except Exception as exc:
        logger.warning("get_portfolio_history failed: %s", exc)
        return None
```

- [ ] **Step 3: Add `get_account_activities()` method to `AlpacaBroker`**

Add this method after `get_portfolio_history` in the `AlpacaBroker` class:

```python
def get_account_activities(self, activity_type: str = "FILL") -> list[dict]:
    """Fetch account activities as plain dicts. Uses direct HTTP (urllib stdlib)
    because alpaca-py's TradingClient does not consistently expose this endpoint
    across SDK versions.

    Each returned dict: {"symbol", "side", "qty", "price", "ts"}.
    Returns [] on any error so callers never crash.
    """
    import json
    import urllib.request

    try:
        self._config.require_alpaca()
        url = (
            f"{self._config.alpaca_base_url}"
            f"/v2/account/activities/{activity_type}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "APCA-API-KEY-ID": self._config.alpaca_api_key or "",
                "APCA-API-SECRET-KEY": self._config.alpaca_secret_key or "",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            activities = json.loads(resp.read())

        result = []
        for a in activities:
            try:
                if a.get("activity_type") != activity_type:
                    continue
                result.append({
                    "symbol": a["symbol"],
                    "side": a["side"].lower(),
                    "qty": float(a["qty"]),
                    "price": float(a["price"]),
                    "ts": a.get("transaction_time", ""),
                })
            except (KeyError, ValueError, TypeError):
                continue
        return result
    except Exception as exc:
        logger.warning("get_account_activities failed: %s", exc)
        return []
```

- [ ] **Step 4: Verify existing tests still pass**

```bash
venv/bin/python -m pytest tests/test_execution.py -v
```

Expected: all existing broker tests pass (no regressions — existing callers pass no `period` arg so the default `"1A"` applies, but existing tests mock `get_portfolio_history` at the Protocol level).

- [ ] **Step 5: Commit**

```bash
git add trader/execution/broker.py
git commit -m "feat(broker): add period param to get_portfolio_history, add get_account_activities"
```

---

## Task 2: Add `get_strategy_signal_counts()` to repo interface and both implementations

**Files:**
- Modify: `trader/portfolio/repository.py`
- Modify: `trader/portfolio/sqlite_repo.py`
- Modify: `trader/portfolio/postgres_repo.py`

- [ ] **Step 1: Add abstract method to `PortfolioRepository`**

In `trader/portfolio/repository.py`, add after `get_runs`:

```python
    @abstractmethod
    def get_strategy_signal_counts(self) -> dict[str, int]:
        """Return signal count per strategy for all auto-mode runs.
        Keys are strategy class names (e.g. "MomentumRSI"); values are signal counts.
        Returns {} if no data."""
```

- [ ] **Step 2: Implement in `SQLiteRepository`**

In `trader/portfolio/sqlite_repo.py`, add after `get_runs`:

```python
    def get_strategy_signal_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.strategy, COUNT(*) AS cnt "
                "FROM signals s JOIN runs r ON s.run_id = r.id "
                "WHERE r.mode = 'auto' GROUP BY r.strategy"
            ).fetchall()
            return {row["strategy"]: row["cnt"] for row in rows}
```

- [ ] **Step 3: Implement in `PostgresRepository`**

In `trader/portfolio/postgres_repo.py`, add after `get_runs`:

```python
    def get_strategy_signal_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT r.strategy, COUNT(*) AS cnt "
                    "FROM signals s JOIN runs r ON s.run_id = r.id "
                    "WHERE r.mode = 'auto' GROUP BY r.strategy"
                )
                return {row["strategy"]: row["cnt"] for row in cur.fetchall()}
```

- [ ] **Step 4: Run existing portfolio tests**

```bash
venv/bin/python -m pytest tests/test_portfolio_repo.py -v
```

Expected: all pass. The new method adds no migration (it queries existing tables).

- [ ] **Step 5: Commit**

```bash
git add trader/portfolio/repository.py trader/portfolio/sqlite_repo.py trader/portfolio/postgres_repo.py
git commit -m "feat(repo): add get_strategy_signal_counts to portfolio repository interface"
```

---

## Task 3: Write failing tests for performance metrics (TDD)

**Files:**
- Create: `tests/test_performance_metrics.py`

- [ ] **Step 1: Create the test file**

```python
"""Unit tests for trader.performance.metrics.

No network, no Alpaca keys — all external calls are injected via mock broker/repo.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from trader.performance.metrics import (
    LiveMetrics,
    _check_thresholds,
    _fifo_round_trips,
    _profit_factor,
    compute_live_metrics,
)


# ---- helpers ----

def _timestamps(n: int) -> list[str]:
    base = date(2026, 1, 1)
    return [(base + timedelta(days=i)).isoformat() + "T00:00:00" for i in range(n)]


def _equity(n: int, start: float = 100_000.0, drift: float = 50.0) -> list[float]:
    """Rising equity curve with enough variance to produce a non-zero Sharpe."""
    import random
    random.seed(42)
    vals = [start]
    for _ in range(n - 1):
        vals.append(vals[-1] + drift + random.gauss(0, 200))
    return vals


def _make_broker(history=None, fills=None):
    broker = MagicMock()
    broker.get_portfolio_history.return_value = history
    broker.get_account_activities.return_value = fills or []
    return broker


def _make_repo(signal_counts=None):
    repo = MagicMock()
    repo.get_strategy_signal_counts.return_value = signal_counts or {}
    return repo


def _make_config():
    return MagicMock()


# ---- _fifo_round_trips ----

def test_fifo_simple_win():
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 1.0, "price": 100.0, "ts": "2026-01-01"},
        {"symbol": "AAPL", "side": "sell", "qty": 1.0, "price": 110.0, "ts": "2026-01-02"},
    ]
    assert _fifo_round_trips(fills) == pytest.approx([10.0])


def test_fifo_simple_loss():
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 2.0, "price": 100.0, "ts": "2026-01-01"},
        {"symbol": "AAPL", "side": "sell", "qty": 2.0, "price":  90.0, "ts": "2026-01-02"},
    ]
    assert _fifo_round_trips(fills) == pytest.approx([-20.0])


def test_fifo_partial_sell_excludes_open():
    """Only the sold portion is counted; remaining open position is excluded."""
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 3.0, "price": 100.0, "ts": "2026-01-01"},
        {"symbol": "AAPL", "side": "sell", "qty": 1.0, "price": 110.0, "ts": "2026-01-02"},
    ]
    pnls = _fifo_round_trips(fills)
    assert len(pnls) == 1
    assert pnls[0] == pytest.approx(10.0)


def test_fifo_multiple_symbols_independent():
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 1.0, "price": 100.0, "ts": "2026-01-01"},
        {"symbol": "MSFT", "side": "buy",  "qty": 1.0, "price": 200.0, "ts": "2026-01-01"},
        {"symbol": "AAPL", "side": "sell", "qty": 1.0, "price": 105.0, "ts": "2026-01-02"},
        {"symbol": "MSFT", "side": "sell", "qty": 1.0, "price": 195.0, "ts": "2026-01-02"},
    ]
    pnls = _fifo_round_trips(fills)
    assert len(pnls) == 2
    assert pytest.approx(5.0) in pnls
    assert pytest.approx(-5.0) in pnls


def test_fifo_only_buys_returns_empty():
    fills = [{"symbol": "AAPL", "side": "buy", "qty": 1.0, "price": 100.0, "ts": "2026-01-01"}]
    assert _fifo_round_trips(fills) == []


def test_fifo_empty_returns_empty():
    assert _fifo_round_trips([]) == []


def test_fifo_fifo_ordering():
    """Second buy at higher price; first lot should match first sell."""
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 1.0, "price": 100.0, "ts": "2026-01-01"},
        {"symbol": "AAPL", "side": "buy",  "qty": 1.0, "price": 120.0, "ts": "2026-01-02"},
        {"symbol": "AAPL", "side": "sell", "qty": 1.0, "price": 115.0, "ts": "2026-01-03"},
    ]
    pnls = _fifo_round_trips(fills)
    assert len(pnls) == 1
    assert pnls[0] == pytest.approx(15.0)  # matched against first buy at 100


# ---- _profit_factor ----

def test_profit_factor_mixed():
    pnls = [10.0, -5.0, 8.0, -3.0]
    assert _profit_factor(pnls) == pytest.approx(18.0 / 8.0)


def test_profit_factor_all_wins_returns_inf():
    assert _profit_factor([10.0, 5.0]) == float("inf")


def test_profit_factor_all_losses_returns_zero():
    assert _profit_factor([-10.0, -5.0]) == 0.0


def test_profit_factor_no_trades_returns_zero():
    assert _profit_factor([]) == 0.0


# ---- _check_thresholds ----

def _passing():
    return dict(
        days_active=61, trade_count=101, sharpe=1.1,
        max_drawdown=-0.10, win_rate=0.50, profit_factor=1.6,
    )


def test_check_thresholds_all_pass():
    assert _check_thresholds(**_passing()) == []


def test_check_thresholds_sharpe_fail():
    kw = {**_passing(), "sharpe": 0.5}
    failures = _check_thresholds(**kw)
    assert any("Sharpe" in f for f in failures)


def test_check_thresholds_drawdown_fail():
    kw = {**_passing(), "max_drawdown": -0.20}
    failures = _check_thresholds(**kw)
    assert any("drawdown" in f for f in failures)


def test_check_thresholds_days_fail():
    kw = {**_passing(), "days_active": 30}
    failures = _check_thresholds(**kw)
    assert any("days" in f for f in failures)


def test_check_thresholds_trades_fail():
    kw = {**_passing(), "trade_count": 5}
    failures = _check_thresholds(**kw)
    assert any("round-trips" in f for f in failures)


def test_check_thresholds_win_rate_fail():
    kw = {**_passing(), "win_rate": 0.30}
    failures = _check_thresholds(**kw)
    assert any("win rate" in f for f in failures)


def test_check_thresholds_profit_factor_fail():
    kw = {**_passing(), "profit_factor": 1.1}
    failures = _check_thresholds(**kw)
    assert any("profit factor" in f for f in failures)


def test_check_thresholds_profit_factor_inf_passes():
    """Infinite profit factor (all wins) should not trigger a failure."""
    kw = {**_passing(), "profit_factor": float("inf")}
    assert _check_thresholds(**kw) == []


def test_check_thresholds_multiple_failures():
    kw = {**_passing(), "sharpe": 0.3, "win_rate": 0.30}
    assert len(_check_thresholds(**kw)) >= 2


# ---- compute_live_metrics ----

def test_compute_insufficient_data_no_history():
    broker = _make_broker(history=None)
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.verdict == "INSUFFICIENT_DATA"
    assert result.days_active == 0


def test_compute_insufficient_data_single_equity_point():
    broker = _make_broker(
        history={"equity": [100_000.0], "timestamp": ["2026-01-01T00:00:00"]}
    )
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.verdict == "INSUFFICIENT_DATA"


def test_compute_fail_no_trades():
    n = 90
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": _timestamps(n)},
        fills=[],
    )
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.verdict == "FAIL"
    assert result.trade_count == 0
    assert any("round-trips" in f for f in result.failing_checks)


def test_compute_strategy_signals_passed_through():
    n = 90
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": _timestamps(n)},
        fills=[],
    )
    repo = _make_repo(signal_counts={"MomentumRSI": 42, "MACrossover": 18})
    result = compute_live_metrics(_make_config(), broker, repo)
    assert result.strategy_signals == {"MomentumRSI": 42, "MACrossover": 18}


def test_compute_metrics_populated_from_equity_curve():
    n = 90
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": _timestamps(n)},
        fills=[],
    )
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.days_active == n - 1
    assert isinstance(result.sharpe, float)
    assert result.max_drawdown <= 0.0


def test_compute_profit_factor_from_fills():
    n = 90
    fills = [
        {"symbol": "AAPL", "side": "buy",  "qty": 1.0, "price": 100.0, "ts": "2026-01-01T10:00:00"},
        {"symbol": "AAPL", "side": "sell", "qty": 1.0, "price": 110.0, "ts": "2026-01-02T10:00:00"},
    ]
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": _timestamps(n)},
        fills=fills,
    )
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.trade_count == 1
    assert result.win_rate == pytest.approx(1.0)
    assert result.profit_factor == float("inf")


def test_compute_benchmark_none_when_fetch_fails(monkeypatch):
    """Benchmark failure must not block verdict computation."""
    import trader.performance.metrics as m
    monkeypatch.setattr(m, "_benchmark_return", lambda *a, **kw: None)
    n = 90
    broker = _make_broker(
        history={"equity": _equity(n), "timestamp": _timestamps(n)},
        fills=[],
    )
    result = compute_live_metrics(_make_config(), broker, _make_repo())
    assert result.benchmark_spy_return is None
    assert result.benchmark_btc_return is None
    assert result.verdict in ("PASS", "FAIL")  # not INSUFFICIENT_DATA
```

- [ ] **Step 2: Run tests to confirm they all fail (module doesn't exist yet)**

```bash
venv/bin/python -m pytest tests/test_performance_metrics.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'trader.performance'`

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_performance_metrics.py
git commit -m "test(performance): add failing unit tests for live metrics module"
```

---

## Task 4: Implement `trader/performance/metrics.py` (make tests pass)

**Files:**
- Create: `trader/performance/__init__.py`
- Create: `trader/performance/metrics.py`

- [ ] **Step 1: Create package marker**

Create `trader/performance/__init__.py` (empty):

```python
```

- [ ] **Step 2: Create `trader/performance/metrics.py`**

```python
"""Live paper trading performance metrics.

Data sources (injected for testability):
  broker.get_portfolio_history(period="1A")   → equity curve
  broker.get_account_activities("FILL")       → fills for win rate / profit factor
  repo.get_strategy_signal_counts()           → per-strategy signal counts
  Alpaca daily bars (SPY, BTC/USD)            → benchmark returns (informational)

Benchmark returns do NOT gate the verdict — they are display context only.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

TRADING_DAYS = 252

MIN_SHARPE = 1.0
MAX_DRAWDOWN = -0.15
MIN_PROFIT_FACTOR = 1.5
MIN_WIN_RATE = 0.45
MIN_TRADES = 100
MIN_DAYS = 60


@dataclass(frozen=True)
class LiveMetrics:
    days_active: int
    trade_count: int
    sharpe: float
    max_drawdown: float
    win_rate: float
    profit_factor: float        # float("inf") when all trades win; 0.0 when no closed trades
    total_return: float
    benchmark_spy_return: float | None
    benchmark_btc_return: float | None
    verdict: str                # "PASS" | "FAIL" | "INSUFFICIENT_DATA"
    failing_checks: list[str]
    strategy_signals: dict[str, int]


# ---- internal helpers ----

def _sharpe(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    returns = equity.pct_change().dropna()
    std = returns.std()
    if std == 0 or np.isnan(std):
        return 0.0
    return float(returns.mean() / std * np.sqrt(TRADING_DAYS))


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    return float(drawdown.min())


def _fifo_round_trips(fills: list[dict]) -> list[float]:
    """FIFO-match buy fills to sell fills per symbol.

    Returns a list of P&L values (one per matched lot). Open positions
    (buys with no matching sell yet) are excluded — only closed round-trips count.
    """
    buy_queues: dict[str, deque] = defaultdict(deque)
    pnls: list[float] = []

    for fill in sorted(fills, key=lambda f: f["ts"]):
        symbol = fill["symbol"]
        qty = float(fill["qty"])
        price = float(fill["price"])

        if fill["side"] == "buy":
            buy_queues[symbol].append([qty, price])
        elif fill["side"] == "sell":
            remaining = qty
            while remaining > 1e-9 and buy_queues[symbol]:
                lot = buy_queues[symbol][0]
                matched = min(lot[0], remaining)
                pnls.append((price - lot[1]) * matched)
                remaining -= matched
                lot[0] -= matched
                if lot[0] < 1e-9:
                    buy_queues[symbol].popleft()

    return pnls


def _profit_factor(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _benchmark_return(symbol: str, start: datetime, end: datetime, config) -> float | None:
    try:
        from trader.data.alpaca_bars import get_daily_bars
        bars = get_daily_bars(symbol, start=start, end=end, config=config)
        if bars is None or len(bars) < 2:
            return None
        return float(bars["close"].iloc[-1] / bars["close"].iloc[0]) - 1.0
    except Exception:
        return None


def _check_thresholds(
    days_active: int,
    trade_count: int,
    sharpe: float,
    max_drawdown: float,
    win_rate: float,
    profit_factor: float,
) -> list[str]:
    failures = []
    if days_active < MIN_DAYS:
        failures.append(f"only {days_active} days active (need ≥{MIN_DAYS})")
    if trade_count < MIN_TRADES:
        failures.append(f"only {trade_count} round-trips (need ≥{MIN_TRADES})")
    if sharpe < MIN_SHARPE:
        failures.append(f"Sharpe {sharpe:.2f} < {MIN_SHARPE}")
    if max_drawdown < MAX_DRAWDOWN:
        failures.append(f"max drawdown {max_drawdown:.1%} < {MAX_DRAWDOWN:.1%}")
    if win_rate < MIN_WIN_RATE:
        failures.append(f"win rate {win_rate:.1%} < {MIN_WIN_RATE:.1%}")
    if profit_factor != float("inf") and profit_factor < MIN_PROFIT_FACTOR:
        failures.append(f"profit factor {profit_factor:.2f} < {MIN_PROFIT_FACTOR}")
    return failures


def _parse_ts(ts_str: str) -> datetime:
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


# ---- public API ----

def compute_live_metrics(config, broker, repo) -> LiveMetrics:
    """Compute live paper trading metrics from real broker data.

    All arguments are injected so this function can be unit-tested without
    a network connection (pass mock broker + repo).
    """
    _insufficient = LiveMetrics(
        days_active=0, trade_count=0, sharpe=0.0, max_drawdown=0.0,
        win_rate=0.0, profit_factor=0.0, total_return=0.0,
        benchmark_spy_return=None, benchmark_btc_return=None,
        verdict="INSUFFICIENT_DATA", failing_checks=[], strategy_signals={},
    )

    history = broker.get_portfolio_history(period="1A")
    if not history or len(history.get("equity", [])) < 2:
        return _insufficient

    equity = pd.Series([float(e) for e in history["equity"]])
    timestamps = history["timestamp"]
    ts_start = _parse_ts(timestamps[0])
    ts_end = _parse_ts(timestamps[-1])
    days_active = max(0, (ts_end - ts_start).days)

    start_eq = float(equity.iloc[0])
    end_eq = float(equity.iloc[-1])
    total_return = (end_eq / start_eq - 1.0) if start_eq > 0 else 0.0
    sharpe = _sharpe(equity)
    max_dd = _max_drawdown(equity)

    fills = broker.get_account_activities(activity_type="FILL")
    pnls = _fifo_round_trips(fills)
    trade_count = len(pnls)
    win_rate = sum(1 for p in pnls if p > 0) / trade_count if trade_count > 0 else 0.0
    pf = _profit_factor(pnls)

    spy_return = _benchmark_return("SPY", ts_start, ts_end, config)
    btc_return = _benchmark_return("BTC/USD", ts_start, ts_end, config)

    signals = repo.get_strategy_signal_counts()

    failures = _check_thresholds(days_active, trade_count, sharpe, max_dd, win_rate, pf)
    verdict = "PASS" if not failures else "FAIL"

    return LiveMetrics(
        days_active=days_active,
        trade_count=trade_count,
        sharpe=sharpe,
        max_drawdown=max_dd,
        win_rate=win_rate,
        profit_factor=pf,
        total_return=total_return,
        benchmark_spy_return=spy_return,
        benchmark_btc_return=btc_return,
        verdict=verdict,
        failing_checks=failures,
        strategy_signals=signals,
    )
```

- [ ] **Step 3: Run the tests**

```bash
venv/bin/python -m pytest tests/test_performance_metrics.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Run full test suite to check for regressions**

```bash
venv/bin/python -m pytest -v
```

Expected: all existing tests continue to pass.

- [ ] **Step 5: Commit**

```bash
git add trader/performance/__init__.py trader/performance/metrics.py
git commit -m "feat(performance): implement live metrics computation module"
```

---

## Task 5: API route + register in main.py

**Files:**
- Create: `api/routes/performance.py`
- Modify: `api/main.py`

- [ ] **Step 1: Create `api/routes/performance.py`**

```python
"""Performance metrics route — live paper trading scorecard."""
from __future__ import annotations

import asyncio
import logging
import math
import time

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_broker, get_config, get_current_user, get_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/performance", tags=["performance"])

_CACHE_TTL = 300  # 5 minutes
_cache: dict = {}


def _serialize(result) -> dict:
    """Convert LiveMetrics to a JSON-safe dict. float('inf') → null."""
    def _safe(v):
        if v is None:
            return None
        if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
            return None
        return v

    return {
        "days_active": result.days_active,
        "trade_count": result.trade_count,
        "sharpe": _safe(result.sharpe),
        "max_drawdown": _safe(result.max_drawdown),
        "win_rate": _safe(result.win_rate),
        "profit_factor": _safe(result.profit_factor),
        "total_return": _safe(result.total_return),
        "benchmark_spy_return": _safe(result.benchmark_spy_return),
        "benchmark_btc_return": _safe(result.benchmark_btc_return),
        "verdict": result.verdict,
        "failing_checks": result.failing_checks,
        "strategy_signals": result.strategy_signals,
    }


def _compute_sync(config, broker, repo) -> dict:
    from trader.performance.metrics import compute_live_metrics
    result = compute_live_metrics(config, broker, repo)
    return _serialize(result)


@router.get("")
async def get_performance(username: str = Depends(get_current_user)):
    now = time.monotonic()
    if _cache.get("computed_at", 0) + _CACHE_TTL > now:
        return _cache["result"]

    try:
        config = get_config()
        broker = get_broker()
        repo = get_repo()

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _compute_sync, config, broker, repo)

        _cache["result"] = data
        _cache["computed_at"] = now
        return data

    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception:
        logger.exception("performance compute failed")
        return {
            "days_active": 0, "trade_count": 0, "sharpe": 0.0,
            "max_drawdown": 0.0, "win_rate": 0.0, "profit_factor": None,
            "total_return": 0.0, "benchmark_spy_return": None,
            "benchmark_btc_return": None, "verdict": "INSUFFICIENT_DATA",
            "failing_checks": [], "strategy_signals": {},
        }
```

- [ ] **Step 2: Register the router in `api/main.py`**

Add the import after the existing router imports:

```python
from api.routes.performance import router as performance_router
```

Add the router registration after the existing `app.include_router` calls (before the WebSocket section):

```python
app.include_router(performance_router, prefix="/api")
```

- [ ] **Step 3: Verify the server starts**

```bash
venv/bin/python -m api.main &
sleep 2
curl -s http://localhost:8000/ | python3 -m json.tool
kill %1
```

Expected: `{"status": "ok"}` — server starts without import errors.

- [ ] **Step 4: Commit**

```bash
git add api/routes/performance.py api/main.py
git commit -m "feat(api): add GET /api/performance route with 5-min cache"
```

---

## Task 6: CLI script

**Files:**
- Create: `scripts/performance_tracker.py`

- [ ] **Step 1: Create `scripts/performance_tracker.py`**

```python
"""Live paper trading performance report — CLI entry point.

Pulls from Alpaca (equity curve + fills) and Supabase (signal counts) to compute
performance metrics and print a PASS/FAIL go-live verdict.

Usage:
    python scripts/performance_tracker.py

Exit codes:
    0 — PASS: all thresholds met
    1 — FAIL or INSUFFICIENT_DATA: below threshold or no data yet
    2 — config error (missing Alpaca keys)
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _check_mark(passing: bool) -> str:
    return "✓" if passing else "✗"


def _fmt_pct(v: float) -> str:
    return f"{v:+.1%}"


def main() -> int:
    try:
        from trader.config import load_config
        config = load_config()
        config.require_alpaca()
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2

    try:
        from trader.execution.broker import AlpacaBroker
        broker = AlpacaBroker(config)

        if config.database_url:
            from trader.portfolio.postgres_repo import PostgresRepository
            repo = PostgresRepository(config.database_url)
        else:
            from trader.portfolio.sqlite_repo import SQLiteRepository
            repo = SQLiteRepository(config.portfolio_db_path)

        from trader.performance.metrics import (
            MAX_DRAWDOWN, MIN_DAYS, MIN_PROFIT_FACTOR, MIN_SHARPE,
            MIN_TRADES, MIN_WIN_RATE, compute_live_metrics,
        )
        m = compute_live_metrics(config, broker, repo)
    except Exception as exc:
        print(f"ERROR: could not compute metrics — {exc}")
        return 2

    sep = "=" * 60
    print(sep)
    print("Live Paper Trading — Performance Report")
    print(sep)

    if m.verdict == "INSUFFICIENT_DATA":
        print("INSUFFICIENT DATA — run the scheduler in auto mode to populate.")
        return 1

    def _row(label, value, threshold_str, passing):
        mark = _check_mark(passing)
        print(f"{label:<20}: {value:>10}  (threshold {threshold_str})  {mark}")

    _row("Days active", m.days_active, f"≥{MIN_DAYS}", m.days_active >= MIN_DAYS)
    _row("Trades (round-trips)", m.trade_count, f"≥{MIN_TRADES}", m.trade_count >= MIN_TRADES)
    _row("Sharpe", f"{m.sharpe:.2f}", f"≥{MIN_SHARPE}", m.sharpe >= MIN_SHARPE)
    _row(
        "Max drawdown",
        f"{m.max_drawdown:.1%}",
        f"≤{abs(MAX_DRAWDOWN):.0%}",
        m.max_drawdown >= MAX_DRAWDOWN,
    )
    _row("Win rate", f"{m.win_rate:.1%}", f"≥{MIN_WIN_RATE:.0%}", m.win_rate >= MIN_WIN_RATE)
    pf_display = "∞" if math.isinf(m.profit_factor) else f"{m.profit_factor:.2f}"
    pf_pass = math.isinf(m.profit_factor) or m.profit_factor >= MIN_PROFIT_FACTOR
    _row("Profit factor", pf_display, f"≥{MIN_PROFIT_FACTOR}", pf_pass)

    print()
    print("Benchmark comparison  (informational — not gated)")
    print(f"  Portfolio  : {_fmt_pct(m.total_return)}")
    print(f"  SPY        : {_fmt_pct(m.benchmark_spy_return) if m.benchmark_spy_return is not None else 'unavailable'}")
    print(f"  BTC/USD    : {_fmt_pct(m.benchmark_btc_return) if m.benchmark_btc_return is not None else 'unavailable'}")

    if m.strategy_signals:
        print()
        print("Strategy signals (V1 — counts only, not P&L)")
        for strategy, count in sorted(m.strategy_signals.items(), key=lambda x: -x[1]):
            print(f"  {strategy:<20}: {count} signals")

    if m.failing_checks:
        print()
        print("Failing checks:")
        for reason in m.failing_checks:
            print(f"  • {reason}")

    print()
    print(sep)
    print(f"GO-LIVE VERDICT: {m.verdict}")
    print(sep)

    return 0 if m.verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify the script is importable (no syntax errors)**

```bash
venv/bin/python -c "import scripts.performance_tracker" 2>&1 || venv/bin/python -m py_compile scripts/performance_tracker.py && echo "OK"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/performance_tracker.py
git commit -m "feat(scripts): add performance_tracker CLI with go-live verdict"
```

---

## Task 7: Frontend — api.ts + Performance.tsx + nav + route

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Create: `frontend/src/pages/Performance.tsx`
- Modify: `frontend/src/components/ProtectedLayout.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Add types and `getPerformance()` to `frontend/src/lib/api.ts`**

Add after the existing portfolio section:

```typescript
// ---- performance ----
export const getPerformance = () => api.get<PerformanceMetrics>('/api/performance')

// ---- types ----
export interface PerformanceMetrics {
  days_active: number
  trade_count: number
  sharpe: number
  max_drawdown: number
  win_rate: number
  profit_factor: number | null   // null when infinity or no data
  total_return: number
  benchmark_spy_return: number | null
  benchmark_btc_return: number | null
  verdict: 'PASS' | 'FAIL' | 'INSUFFICIENT_DATA'
  failing_checks: string[]
  strategy_signals: Record<string, number>
}
```

- [ ] **Step 2: Create `frontend/src/pages/Performance.tsx`**

```tsx
import { useQuery } from '@tanstack/react-query'
import { getPerformance, type PerformanceMetrics } from '../lib/api'

const THRESHOLDS = {
  sharpe: { min: 1.0, label: '≥1.0' },
  max_drawdown: { max: -0.15, label: '≤15%' },
  win_rate: { min: 0.45, label: '≥45%' },
  profit_factor: { min: 1.5, label: '≥1.5' },
  trade_count: { min: 100, label: '≥100' },
  days_active: { min: 60, label: '≥60' },
}

function passes(metric: keyof typeof THRESHOLDS, value: number | null): boolean | null {
  if (value === null) return null
  const t = THRESHOLDS[metric]
  if ('min' in t) return value >= t.min
  if ('max' in t) return value >= t.max
  return null
}

function MetricTile({
  label,
  value,
  threshold,
  passing,
  format,
}: {
  label: string
  value: string
  threshold: string
  passing: boolean | null
  format?: string
}) {
  const border =
    passing === null
      ? 'border-slate-600'
      : passing
      ? 'border-green-500'
      : 'border-red-500'
  const mark =
    passing === null ? '' : passing ? '✓' : '✗'
  const markColor = passing ? 'text-green-400' : 'text-red-400'

  return (
    <div className={`bg-slate-800 rounded-xl p-4 border-2 ${border} flex flex-col gap-1`}>
      <div className="text-slate-400 text-xs uppercase tracking-wide">{label}</div>
      <div className="text-white text-2xl font-bold font-mono">{value}</div>
      <div className="flex items-center gap-1 text-xs">
        <span className="text-slate-500">{threshold}</span>
        {mark && <span className={`font-bold ${markColor}`}>{mark}</span>}
      </div>
    </div>
  )
}

function VerdictBanner({ verdict }: { verdict: string }) {
  const styles: Record<string, string> = {
    PASS: 'bg-green-900 border-green-500 text-green-200',
    FAIL: 'bg-red-900 border-red-500 text-red-200',
    INSUFFICIENT_DATA: 'bg-yellow-900 border-yellow-500 text-yellow-200',
  }
  const labels: Record<string, string> = {
    PASS: '✓ GO-LIVE VERDICT: PASS',
    FAIL: '✗ GO-LIVE VERDICT: FAIL',
    INSUFFICIENT_DATA: '⚠ INSUFFICIENT DATA',
  }
  return (
    <div className={`rounded-xl border-2 px-6 py-4 font-bold text-lg ${styles[verdict] ?? styles.INSUFFICIENT_DATA}`}>
      {labels[verdict] ?? verdict}
    </div>
  )
}

function BenchmarkRow({ label, value }: { label: string; value: number | null }) {
  if (value === null) return (
    <div className="flex items-center justify-between py-2 border-t border-slate-700">
      <span className="text-slate-400 text-sm">{label}</span>
      <span className="text-slate-500 text-sm font-mono">unavailable</span>
    </div>
  )
  const color = value >= 0 ? 'text-green-400' : 'text-red-400'
  return (
    <div className="flex items-center justify-between py-2 border-t border-slate-700">
      <span className="text-slate-300 text-sm">{label}</span>
      <span className={`font-mono font-bold text-sm ${color}`}>
        {value >= 0 ? '+' : ''}{(value * 100).toFixed(1)}%
      </span>
    </div>
  )
}

export default function Performance() {
  const { data: m, isLoading } = useQuery({
    queryKey: ['performance'],
    queryFn: () => getPerformance().then((r) => r.data),
    refetchInterval: 300_000,  // 5 min — matches server cache TTL
  })

  if (isLoading) {
    return <p className="text-slate-400">Loading performance data…</p>
  }

  if (!m || m.verdict === 'INSUFFICIENT_DATA') {
    return (
      <div className="flex flex-col gap-6">
        <h1 className="text-2xl font-bold text-white">Performance</h1>
        <div className="bg-slate-800 rounded-xl p-6 text-slate-400 border border-slate-700">
          Not enough paper trading data yet — run the scheduler in auto mode to populate.
        </div>
      </div>
    )
  }

  const pfDisplay = m.profit_factor === null ? '∞' : m.profit_factor.toFixed(2)
  const pfPasses = m.profit_factor === null ? true : m.profit_factor >= THRESHOLDS.profit_factor.min

  const sortedSignals = Object.entries(m.strategy_signals).sort((a, b) => b[1] - a[1])
  const maxSignals = sortedSignals[0]?.[1] ?? 1

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-bold text-white">Performance</h1>

      <VerdictBanner verdict={m.verdict} />

      {m.failing_checks.length > 0 && (
        <div className="bg-red-950 border border-red-800 rounded-xl px-4 py-3">
          <div className="text-red-300 text-sm font-semibold mb-1">Failing checks:</div>
          <ul className="list-disc list-inside text-red-400 text-sm space-y-0.5">
            {m.failing_checks.map((f) => <li key={f}>{f}</li>)}
          </ul>
        </div>
      )}

      {/* Metric tiles */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        <MetricTile
          label="Sharpe"
          value={m.sharpe.toFixed(2)}
          threshold={THRESHOLDS.sharpe.label}
          passing={passes('sharpe', m.sharpe)}
        />
        <MetricTile
          label="Max Drawdown"
          value={`${(m.max_drawdown * 100).toFixed(1)}%`}
          threshold={THRESHOLDS.max_drawdown.label}
          passing={passes('max_drawdown', m.max_drawdown)}
        />
        <MetricTile
          label="Win Rate"
          value={`${(m.win_rate * 100).toFixed(1)}%`}
          threshold={THRESHOLDS.win_rate.label}
          passing={passes('win_rate', m.win_rate)}
        />
        <MetricTile
          label="Profit Factor"
          value={pfDisplay}
          threshold={THRESHOLDS.profit_factor.label}
          passing={pfPasses}
        />
        <MetricTile
          label="Trades"
          value={String(m.trade_count)}
          threshold={THRESHOLDS.trade_count.label}
          passing={passes('trade_count', m.trade_count)}
        />
        <MetricTile
          label="Days Active"
          value={String(m.days_active)}
          threshold={THRESHOLDS.days_active.label}
          passing={passes('days_active', m.days_active)}
        />
      </div>

      {/* Benchmark */}
      <section>
        <h2 className="text-lg font-bold text-white mb-3">
          Benchmark Comparison
          <span className="ml-2 text-xs font-normal text-slate-500">(informational — not gated)</span>
        </h2>
        <div className="bg-slate-800 rounded-xl px-4 border border-slate-700">
          <BenchmarkRow label="Portfolio" value={m.total_return} />
          <BenchmarkRow label="SPY" value={m.benchmark_spy_return} />
          <BenchmarkRow label="BTC/USD" value={m.benchmark_btc_return} />
        </div>
      </section>

      {/* Strategy signals */}
      {sortedSignals.length > 0 && (
        <section>
          <h2 className="text-lg font-bold text-white mb-3">
            Strategy Signal Activity
            <span className="ml-2 text-xs font-normal text-slate-500">(V1 — counts only, not P&L)</span>
          </h2>
          <div className="bg-slate-800 rounded-xl px-4 border border-slate-700">
            {sortedSignals.map(([strategy, count]) => (
              <div key={strategy} className="flex items-center gap-4 py-3 border-t first:border-0 border-slate-700">
                <span className="text-slate-300 text-sm w-36 shrink-0">{strategy}</span>
                <div className="flex-1 bg-slate-700 rounded-full h-2">
                  <div
                    className="bg-blue-500 h-2 rounded-full"
                    style={{ width: `${(count / maxSignals) * 100}%` }}
                  />
                </div>
                <span className="text-slate-400 text-sm font-mono w-12 text-right">{count}</span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
```

- [ ] **Step 3: Add Performance to nav in `ProtectedLayout.tsx`**

Replace the `NAV` constant:

```typescript
const NAV = [
  { to: '/portfolio', label: 'Portfolio' },
  { to: '/performance', label: 'Performance' },
  { to: '/approvals', label: 'Approvals' },
  { to: '/analysis', label: 'Analysis' },
  { to: '/controls', label: 'Controls' },
]
```

- [ ] **Step 4: Add route to `App.tsx`**

Add the import:

```typescript
import Performance from './pages/Performance'
```

Add the route inside `<Route element={<ProtectedLayout />}>`:

```tsx
<Route path="/performance" element={<Performance />} />
```

The complete routes block should be:

```tsx
<Route element={<ProtectedLayout />}>
  <Route path="/portfolio" element={<Portfolio />} />
  <Route path="/performance" element={<Performance />} />
  <Route path="/approvals" element={<Approvals />} />
  <Route path="/analysis" element={<Analysis />} />
  <Route path="/controls" element={<Controls />} />
  <Route path="/" element={<Navigate to="/portfolio" replace />} />
</Route>
```

- [ ] **Step 5: Build frontend to check for TypeScript errors**

```bash
cd frontend && npm run build 2>&1 | tail -20
```

Expected: build succeeds with no TypeScript errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/pages/Performance.tsx \
        frontend/src/components/ProtectedLayout.tsx frontend/src/App.tsx
git commit -m "feat(frontend): add Performance dashboard page with go-live verdict and metric tiles"
```

---

## Task 8: Full integration check

- [ ] **Step 1: Run the full test suite**

```bash
venv/bin/python -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 2: Verify CLI script is runnable**

```bash
venv/bin/python scripts/performance_tracker.py 2>&1 | head -5
```

Expected: prints header line (either report or config error — not a Python traceback).

- [ ] **Step 3: Commit any fixups and tag**

```bash
git add -p  # stage any fixups
git commit -m "chore: performance tracker integration fixups" 2>/dev/null || true
```
