"""The Wheel — sell CSP; on assignment hold shares and sell CC; on called-away, repeat.

Split in two because the `Strategy` base contract (`_decide(bars, asof) -> Signal`) is
stateless and bars-only, but the Wheel's state (which leg is currently open) lives in
the DB and spans multiple ticks/expiries — a single symbol can sit in "assigned" for
weeks waiting for the next CC to be sold.

  - `WheelStrategy` is the *entry trigger* for a flat symbol: reuses `DipRecovery`'s
    dip-detection to decide when to sell the opening CSP. The pipeline checks
    `isinstance(strategy, WheelStrategy)` and routes its buy signal through the options
    broker (CSP sell-to-open) instead of a stock buy.
  - `advance_wheel_state` is the *state-machine* step, called once per tick per
    wheel-enabled symbol regardless of any Signal: given the symbol's open
    `options_positions` rows and current broker share count, decides the next action
    (sell CC after assignment, re-open CSP after being called away). This repeats
    unconditionally once a cycle is started — unlike DipRecovery's own entries, the
    Wheel does not re-check the dip condition to resume a cycle already in motion.
"""
from __future__ import annotations

from dataclasses import dataclass

from trader.strategy.dip_recovery import DipRecovery

Action = str  # "sell_csp" | "sell_cc" | "hold" | "none"


class WheelStrategy(DipRecovery):
    """Same dip-entry trigger as DipRecovery; distinguished only by type so the
    pipeline can route its buy signal to a CSP sell instead of a stock buy."""


@dataclass(frozen=True)
class WheelAction:
    action: Action
    reason: str


def advance_wheel_state(
    symbol: str,
    open_positions: list[dict],
    shares_held: float,
) -> WheelAction:
    """Decide the next Wheel action for `symbol` from its open `options_positions` rows
    (as returned by `PortfolioRepository.get_open_options_positions`) and current broker
    share count. Pure function — no I/O — so it's trivially unit-testable; the caller
    does the repo/broker reads and applies the resulting action.
    """
    assigned = [p for p in open_positions if p["wheel_state"] == "assigned"]
    if assigned:
        if shares_held >= 100:
            return WheelAction("sell_cc", f"{symbol} assigned, {shares_held:.0f} shares held — sell covered call")
        return WheelAction("hold", f"{symbol} assigned but shares not yet settled ({shares_held:.0f})")

    cc_open = [p for p in open_positions if p["wheel_state"] == "cc_open"]
    if cc_open:
        if shares_held < 100:
            # Shares are gone — the call was exercised (called away). Close the row and
            # signal a fresh CSP to restart the cycle.
            return WheelAction("sell_csp", f"{symbol} called away — restarting wheel with new CSP")
        return WheelAction("hold", f"{symbol} covered call open, {shares_held:.0f} shares held")

    csp_open = [p for p in open_positions if p["wheel_state"] == "csp_open"]
    if csp_open:
        return WheelAction("hold", f"{symbol} CSP open, awaiting expiry/assignment")

    # No open wheel row, but >=100 shares held (e.g. the last CC expired worthless
    # instead of being exercised) — the cycle continues, sell a fresh CC.
    if shares_held >= 100:
        return WheelAction("sell_cc", f"{symbol} {shares_held:.0f} shares idle — sell covered call")

    return WheelAction("none", f"{symbol} no open wheel position")


def reconcile_options(options_broker, stock_broker, repo) -> None:
    """Detect assignment/expiry/call-away that happened off-hours (Friday close,
    weekends) — must run before the scheduler's market-hours gate, since options
    expire and settle whether or not the process was ticking at the time. Without
    this, a Wheel cycle would sit stale (still `csp_open`) until the next open tick
    instead of resuming as `assigned`/`called_away`.
    """
    from datetime import date, datetime, timezone

    stock_state = stock_broker.reconcile()
    if stock_state.stale:
        return
    open_positions = repo.get_open_options_positions()
    if not open_positions:
        return
    broker_option_symbols = {p["symbol"] for p in options_broker.open_option_positions()}
    today = datetime.now(timezone.utc).date()

    for pos in open_positions:
        expiry = date.fromisoformat(pos["expiry"])
        if expiry > today or pos["contract_symbol"] in broker_option_symbols:
            continue  # still open / not yet expired

        shares_held = stock_state.positions.get(pos["underlying"], 0.0)
        if pos["wheel_state"] == "cc_open":
            if shares_held < 100:
                repo.update_options_position(pos["contract_symbol"], wheel_state="called_away", status="closed")
            else:
                repo.update_options_position(pos["contract_symbol"], wheel_state="cc_expired", status="closed")
        else:  # csp_open — either CSP-on-dip or the Wheel's opening leg
            if shares_held >= 100:
                # Row stays OPEN with wheel_state=assigned so advance_wheel_state sees
                # it on the next tick and knows to sell the covering call.
                repo.update_options_position(pos["contract_symbol"], wheel_state="assigned", status="open")
            else:
                repo.update_options_position(pos["contract_symbol"], wheel_state="csp_expired", status="closed")


def run_wheel_tick(config, options_broker, stock_broker, repo, gate, kill_switch, underlyings) -> list[str]:
    """Advance the Wheel state machine for each symbol in `underlyings`.

    Called once per scheduler tick, independent of any Signal — covers the two
    transitions DipRecovery's dip trigger never fires for: selling a covered call
    after assignment, and re-opening a CSP after being called away. Returns the
    underlyings that had an order submitted this tick.
    """
    import logging
    from datetime import datetime, timezone

    from trader.execution.options_broker import options_client_order_id_for
    from trader.portfolio.repository import OptionsPositionRow, OrderRow
    from trader.risk.gate import OptionsOrderIntent

    logger = logging.getLogger(__name__)
    acted: list[str] = []
    stock_state = stock_broker.reconcile()
    if stock_state.stale:
        return acted
    from dataclasses import replace as _replace
    total_collateral = sum(p["collateral"] for p in repo.get_open_options_positions())
    stock_state = _replace(stock_state, options_collateral=total_collateral)
    today = datetime.now(timezone.utc).date()

    for underlying in underlyings:
        open_positions = repo.get_open_options_positions(underlying)
        shares_held = stock_state.positions.get(underlying, 0.0)
        action = advance_wheel_state(underlying, open_positions, shares_held)
        if action.action in ("hold", "none"):
            continue

        ref_price = stock_state.avg_entry_prices.get(underlying) or 0.0
        if ref_price <= 0:
            continue  # can't select a strike without a reference price

        if action.action == "sell_cc":
            contract = options_broker.select_cc_contract(underlying, ref_price, shares_held)
            if contract is None:
                continue
            collateral = 100.0 * ref_price
            right = "call"
        else:  # sell_csp — restart after being called away
            budget = config.risk.max_options_allocation_pct * stock_state.equity - stock_state.options_collateral
            if budget <= 0:
                continue
            contract = options_broker.select_csp_contract(underlying, ref_price, budget)
            if contract is None:
                continue
            collateral = contract.strike * 100.0
            right = "put"

        spread = options_broker.check_spread(contract.symbol)
        if spread is not None and spread > config.risk.options_max_spread_pct:
            continue

        decision = gate.evaluate_options_order(
            OptionsOrderIntent(underlying=underlying, collateral=collateral), stock_state, kill_switch,
        )
        if not decision.approved:
            logger.info("wheel gate rejected %s: %s", underlying, decision.reason)
            continue

        client_order_id = options_client_order_id_for(today, underlying, right, "wheel")
        order = options_broker.sell_to_open(contract_symbol=contract.symbol, client_order_id=client_order_id)
        broker_order_id = str(getattr(order, "id", "") or "")
        repo.record_order(OrderRow(
            client_order_id=client_order_id, symbol=contract.symbol, side="sell",
            notional=collateral, status="submitted", broker_order_id=broker_order_id or None,
            strategy_name="WheelStrategy", regime=None, signal_strength=None,
            entry_rationale=action.reason,
        ))
        repo.record_options_position(OptionsPositionRow(
            contract_symbol=contract.symbol, underlying=underlying, option_type=right,
            strike=contract.strike, expiry=contract.expiry.isoformat(),
            opening_order_id=client_order_id, strategy="wheel", collateral=collateral,
            wheel_state="cc_open" if right == "call" else "csp_open", status="open",
        ))
        # Advance options_collateral on stock_state so the next underlying in this
        # loop sees the collateral already committed, enforcing the combined cap
        # across the whole underlyings pass rather than just checking the initial total.
        stock_state = _replace(stock_state, options_collateral=stock_state.options_collateral + collateral)
        acted.append(underlying)

    return acted
