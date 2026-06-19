"""Nightly bandit-weight update batch.

Consumes broker fill dicts (from AlpacaBroker.get_account_activities), joins them
to the orders table via broker_order_id → order_id to recover (strategy, regime)
context, computes FIFO realized P&L per (strategy, regime) arm, then runs one
Thompson Sampling update step per arm and persists the result.

Called by the scheduler (nightly, after market close). Shadow-mode is enforced
upstream via the config flags — this module always writes weights regardless; the
pipeline decides whether to act on them.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from trader.learning.bandit_weights import (
    DEFAULT_WEIGHT,
    WEIGHT_CEIL,
    WEIGHT_FLOOR,
    should_reset,
    thompson_sample,
    update_arm,
)
from trader.learning.ic_metrics import compute_icir, ic_weight_nudge
from trader.portfolio.repository import PortfolioRepository


def compute_pnls_from_fills(
    orders: list[dict],
    fills: list[dict],
) -> dict[tuple[str, str], list[float]]:
    """Match broker fills to orders and compute FIFO realized P&L per (strategy, regime).

    orders: rows from repo.get_orders() — must have broker_order_id, strategy_name, regime
    fills:  dicts from broker.get_account_activities() — must have order_id, symbol, side, qty, price

    Returns only arms that have at least one closed round-trip (buy + matched sell).
    """
    order_lookup: dict[str, dict] = {
        o["broker_order_id"]: o
        for o in orders
        if o.get("broker_order_id") and o.get("strategy_name") and o.get("regime")
    }

    # bucket fills by (strategy, regime, symbol) → buy queue and sell list
    buy_queues: dict[tuple, list[float]] = defaultdict(list)  # fifo prices
    sell_prices: dict[tuple, list[tuple[float, float]]] = defaultdict(list)  # (qty, price)

    for fill in fills:
        order = order_lookup.get(fill.get("order_id", ""))
        if order is None:
            continue
        key = (order["strategy_name"], order["regime"], fill["symbol"])
        if fill["side"] == "buy":
            buy_queues[key].extend([fill["price"]] * int(fill["qty"]))
        elif fill["side"] == "sell":
            sell_prices[key].append((float(fill["qty"]), float(fill["price"])))

    pnls: dict[tuple[str, str], list[float]] = defaultdict(list)

    for key, sells in sell_prices.items():
        strategy, regime, _ = key
        arm = (strategy, regime)
        buy_q = buy_queues.get(key, [])
        for qty, sell_price in sells:
            n = int(qty)
            matched = min(n, len(buy_q))
            if matched == 0:
                continue
            buy_slice = buy_q[:matched]
            buy_q = buy_q[matched:]
            avg_buy = sum(buy_slice) / len(buy_slice)
            pnls[arm].append((sell_price - avg_buy) * matched)
        buy_queues[key] = buy_q

    return dict(pnls)


def update_bandit_weights(
    repo: PortfolioRepository,
    fills: list[dict],
    cycle_index: int = 0,
    every: int = 10,
) -> dict[tuple[str, str], float]:
    """Run one nightly Thompson Sampling update for all active (strategy, regime) arms.

    Returns the final weight map (empty dict if no fills produced P&L data).
    Persists all updated arms to repo.
    """
    if not fills:
        return {}

    orders = repo.get_orders()
    pnls = compute_pnls_from_fills(orders=orders, fills=fills)

    if not pnls:
        return {}

    existing_arms = repo.get_all_bandit_arms()
    result: dict[tuple[str, str], float] = {}

    for arm, arm_pnls in pnls.items():
        alpha, beta, _ = existing_arms.get(arm, (1, 1, 0))
        new_alpha, new_beta = update_arm(alpha, beta, arm_pnls)

        if should_reset(cycle_index, every):
            new_alpha, new_beta = 1, 1
            weight = DEFAULT_WEIGHT
        else:
            weight = thompson_sample(new_alpha, new_beta)

        # Apply IC nudge before writing: fetch stored IC series, compute ICIR, add small delta
        ic_series = repo.get_ic_series(arm[0], arm[1])
        icir = compute_icir(ic_series)
        nudge = ic_weight_nudge(icir)
        if nudge != 0.0:
            weight = float(np.clip(weight + nudge, WEIGHT_FLOOR, WEIGHT_CEIL))
        # Single write with final weight
        repo.save_bandit_arm(arm[0], arm[1], new_alpha, new_beta, cycle_index, weight=weight)
        result[arm] = weight

    return result


def record_ic_observations(
    repo: PortfolioRepository,
    ic_by_arm: dict[tuple[str, str], float],
) -> None:
    """Persist IC observations. Called from scheduler after computing IC from broker fills."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    for (strategy, regime), ic in ic_by_arm.items():
        repo.append_ic_observation(strategy, regime, ic, ts)
