"""Bar-replay engine — long/flat, decide-on-t / fill-on-t+1-open.

This is the keystone that earns "prove edge before real money". Two anti-lookahead
guarantees:

  1. The decision at bar t sees only bars with index <= t. (Enforced upstream by
     Strategy.generate, which truncates before the subclass runs.)
  2. The resulting order fills at the OPEN of bar t+1 — never the close of bar t that
     was used to decide. A strategy therefore cannot trade on information it could not
     have acted on in real time.

Position model is intentionally simple and honest for a cash account: fully invested
(long) or flat. "buy" while flat enters; "sell" while long exits; everything else holds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from trader.backtest.costs import CostModel
from trader.strategy.base import Signal, Strategy


@dataclass
class Trade:
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    shares: float

    @property
    def return_pct(self) -> float:
        return self.exit_price / self.entry_price - 1.0


@dataclass
class BacktestResult:
    equity_curve: pd.Series           # mark-to-market equity per bar
    trades: list[Trade]
    buy_hold_curve: pd.Series         # baseline: buy at first fillable open, hold
    initial_cash: float
    cost_model: CostModel
    fills: list[dict] = field(default_factory=list)  # audit log of every fill


def run_backtest(
    bars: pd.DataFrame,
    strategy: Strategy,
    initial_cash: float = 10_000.0,
    cost_model: CostModel | None = None,
    stop_loss_pct: float | None = None,
    entry_fraction: Callable[[pd.DataFrame], float] | None = None,
) -> BacktestResult:
    """Replay `bars` through `strategy`. `bars` must have a sorted DatetimeIndex and a
    `close` and `open` column.

    `stop_loss_pct` mirrors the live pipeline's stop-loss: when the decision bar's
    close is that fraction or more below the entry fill price, the position is
    force-sold at the next open, overriding the strategy's signal (the strategy is
    not consulted on that bar, matching `_prepare_signal`). None disables it.

    `entry_fraction` is an optional sizing policy (e.g. vol targeting): called with
    the bars visible at the decision (index <= asof), it returns the fraction of
    cash a buy deploys; the remainder stays in cash until the position exits.
    Returns outside (0, 1] are treated as full size — a buggy sizer can never
    produce leverage or a zero/negative position. None means all-in (today's
    behavior).
    """
    if not bars.index.is_monotonic_increasing:
        bars = bars.sort_index()
    cost_model = cost_model or CostModel()

    cash = initial_cash
    shares = 0.0
    trades: list[Trade] = []
    fills: list[dict] = []
    open_entry: dict | None = None
    equity_index: list[pd.Timestamp] = []
    equity_values: list[float] = []

    dates = bars.index
    # Stop at len-1: every decision at i needs a bar i+1 to fill against.
    for i in range(len(dates) - 1):
        asof = dates[i]

        drawdown_from_entry = (
            (float(bars.iloc[i]["close"]) - open_entry["price"]) / open_entry["price"]
            if shares > 0.0 and open_entry is not None
            else 0.0
        )
        if stop_loss_pct is not None and drawdown_from_entry <= -stop_loss_pct:
            signal = Signal(
                strategy.symbol, "sell", 1.0,
                f"stop-loss: close down {drawdown_from_entry:.1%} from entry "
                f"{open_entry['price']:.4f}",
            )
        else:
            signal = strategy.generate(bars, asof)

        next_open = float(bars.iloc[i + 1]["open"])
        fill_date = dates[i + 1]

        if signal.side == "buy" and shares == 0.0 and signal.strength > 0:
            fraction = 1.0
            if entry_fraction is not None:
                requested = float(entry_fraction(bars.iloc[: i + 1]))
                if 0.0 < requested <= 1.0:
                    fraction = requested
            price = cost_model.fill_price(next_open, "buy")
            spend = cash * fraction
            commission = cost_model.commission(spend)
            investable = spend - commission
            if investable > 0:
                shares = investable / price
                cash -= spend
                open_entry = {"date": fill_date, "price": price, "shares": shares}
                fills.append({"date": fill_date, "side": "buy", "price": price,
                              "shares": shares, "reason": signal.reason})

        elif signal.side == "sell" and shares > 0.0:
            price = cost_model.fill_price(next_open, "sell")
            proceeds = shares * price
            commission = cost_model.commission(proceeds)
            cash = proceeds - commission
            trades.append(Trade(
                entry_date=open_entry["date"], entry_price=open_entry["price"],
                exit_date=fill_date, exit_price=price, shares=shares))
            fills.append({"date": fill_date, "side": "sell", "price": price,
                          "shares": shares, "reason": signal.reason})
            shares = 0.0
            open_entry = None

        # Mark to market at bar i+1's close (the state after any fill on i+1).
        mark = cash + shares * float(bars.iloc[i + 1]["close"])
        equity_index.append(fill_date)
        equity_values.append(mark)

    equity_curve = pd.Series(equity_values, index=pd.DatetimeIndex(equity_index),
                             name="equity")
    buy_hold_curve = _buy_hold(bars, initial_cash, cost_model)
    return BacktestResult(
        equity_curve=equity_curve,
        trades=trades,
        buy_hold_curve=buy_hold_curve,
        initial_cash=initial_cash,
        cost_model=cost_model,
        fills=fills,
    )


def _buy_hold(bars: pd.DataFrame, initial_cash: float, cost_model: CostModel) -> pd.Series:
    """Baseline: buy at the first fillable open (bar 1) and hold to the end, with the
    same cost model applied to the single entry."""
    if len(bars) < 2:
        return pd.Series(dtype=float, name="buy_hold")
    entry_price = cost_model.fill_price(float(bars.iloc[1]["open"]), "buy")
    shares = (initial_cash - cost_model.commission(initial_cash)) / entry_price
    closes = bars["close"].iloc[1:]
    return pd.Series(shares * closes.values, index=closes.index, name="buy_hold")
