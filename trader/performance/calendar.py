"""Daily P&L calendar data — equity-curve tile values + FIFO trade drilldown."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone


def _parse_ts(ts_str: str) -> datetime:
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def _fifo_trades_by_date(fills: list[dict], order_map: dict[str, str]) -> dict[str, list[dict]]:
    """FIFO-match buys to sells; group closed lots by close date.

    Buy queue entry: [remaining_qty, open_price, buy_order_id, open_ts].
    Strategy is resolved from the buy fill's order_id first, sell fill's order_id second.
    """
    buy_queues: dict[str, deque] = defaultdict(deque)
    trades_by_date: dict[str, list[dict]] = defaultdict(list)

    for fill in sorted(fills, key=lambda f: f["ts"]):
        symbol = fill["symbol"]
        qty = float(fill["qty"])
        price = float(fill["price"])
        order_id = fill.get("order_id")

        if fill["side"] == "buy":
            buy_queues[symbol].append([qty, price, order_id, fill["ts"]])
        elif fill["side"] == "sell":
            remaining = qty
            close_ts = fill["ts"]
            close_date = close_ts[:10]

            while remaining > 1e-9 and buy_queues[symbol]:
                lot = buy_queues[symbol][0]
                lot_qty, open_price, buy_order_id, open_ts = lot[0], lot[1], lot[2], lot[3]
                matched = min(lot_qty, remaining)

                pnl = (price - open_price) * matched
                pnl_pct = (price - open_price) / open_price if open_price else 0.0

                strategy = (
                    order_map.get(buy_order_id)
                    or order_map.get(order_id)
                    or None
                )

                trades_by_date[close_date].append({
                    "symbol": symbol,
                    "strategy": strategy,
                    "pnl": round(pnl, 4),
                    "pnl_pct": round(pnl_pct, 6),
                    "qty": round(matched, 6),
                    "open_price": open_price,
                    "close_price": price,
                    "open_date": open_ts[:10],
                    "close_date": close_date,
                })

                remaining -= matched
                lot[0] -= matched
                if lot[0] < 1e-9:
                    buy_queues[symbol].popleft()

    return dict(trades_by_date)


def compute_calendar_data(broker, repo) -> list[dict]:
    """Return sorted list of CalendarDay dicts for all available history."""
    from datetime import date as _date

    history = broker.get_portfolio_history(period="1A") or {}
    timestamps = history.get("timestamp", [])
    equities = history.get("equity", [])

    equity_by_date: dict[str, tuple] = {}
    for i in range(1, len(timestamps)):
        date_str = timestamps[i][:10]
        prev_eq = float(equities[i - 1]) if equities[i - 1] is not None else None
        curr_eq = float(equities[i]) if equities[i] is not None else None
        if prev_eq and curr_eq and prev_eq > 0:
            equity_by_date[date_str] = (
                round((curr_eq - prev_eq) / prev_eq, 6),
                round(curr_eq - prev_eq, 4),
            )
        else:
            equity_by_date[date_str] = (None, None)

    # Inject today's live intraday P&L — daily history bar won't exist until EOD.
    today_str = _date.today().isoformat()
    if today_str not in equity_by_date:
        try:
            state = broker.reconcile()
            if state.daily_pnl_pct is not None and state.equity > 0:
                last_eq = state.equity / (1 + state.daily_pnl_pct)
                pnl_amount = state.equity - last_eq
                equity_by_date[today_str] = (
                    round(state.daily_pnl_pct, 6),
                    round(pnl_amount, 4),
                )
        except Exception:
            pass

    fills = broker.get_account_activities(activity_type="FILL")
    orders = repo.get_orders()
    order_map = {
        row["broker_order_id"]: row.get("strategy_name")
        for row in orders
        if row.get("broker_order_id") and row.get("strategy_name")
    }

    trades_by_date = _fifo_trades_by_date(fills, order_map)

    all_dates = set(equity_by_date) | set(trades_by_date)

    result = []
    for date_str in sorted(all_dates):
        pnl_pct, pnl_amount = equity_by_date.get(date_str, (None, None))
        result.append({
            "date": date_str,
            "pnl_pct": pnl_pct,
            "pnl_amount": pnl_amount,
            "trades": trades_by_date.get(date_str, []),
        })

    return result
