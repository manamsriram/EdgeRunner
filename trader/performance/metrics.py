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
    equity = equity.replace(0, np.nan).dropna()
    if len(equity) < 2:
        return 0.0
    returns = equity.pct_change().dropna()
    if returns.empty:
        return 0.0
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

    fills = broker.get_account_activities(activity_type="FILL")
    pnls = _fifo_round_trips(fills)
    trade_count = sum(1 for f in fills if f.get("side") == "sell")

    raw_equity = [float(e) if e is not None else np.nan for e in history["equity"]]
    timestamps = history["timestamp"]
    ts_end = _parse_ts(timestamps[-1])

    # Trim equity curve to start at the bot's actual first trade so Sharpe/drawdown/
    # days_active reflect live-trading performance, not the flat pre-trading period
    # (or, if the account predates this bot, months of unrelated history).
    #
    # Prefer the local order ledger over Alpaca's fills endpoint: it's our own data,
    # recorded synchronously on every submit (pipeline.py), so it can't go stale or
    # empty the way an external API call can — and a single point of failure there
    # (e.g. get_account_activities silently failing) must not also corrupt this.
    start_candidates = []
    if fills:
        start_candidates.append(_parse_ts(min(fills, key=lambda f: f["ts"])["ts"]))
    orders = repo.get_orders()
    order_timestamps = [o["ts"] for o in orders if o.get("ts")]
    if order_timestamps:
        start_candidates.append(_parse_ts(min(order_timestamps)))

    if start_candidates:
        first_trade_ts = min(start_candidates)
        trim_idx = next(
            (i for i, ts in enumerate(timestamps) if _parse_ts(ts) >= first_trade_ts),
            0,
        )
    else:
        trim_idx = 0

    equity = pd.Series(raw_equity[trim_idx:])
    ts_start = _parse_ts(timestamps[trim_idx])
    days_active = max(0, (ts_end - ts_start).days)

    start_eq = float(equity.iloc[0])
    end_eq = float(equity.iloc[-1])
    total_return = (end_eq / start_eq - 1.0) if start_eq > 0 else 0.0
    sharpe = _sharpe(equity)
    max_dd = _max_drawdown(equity)
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
