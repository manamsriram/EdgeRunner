"""The pipeline spine: tick → data → strategy → overlay → risk gate → decision gate → execute/queue → record.

The decision gate is the ONLY difference between AUTONOMY=manual and AUTONOMY=auto:
  manual  → risk-approved orders become proposals in the repo queue
  auto    → risk-approved orders execute directly via the broker

Both paths share the same risk gate, kill switch, and broker adapter, so flipping
AUTONOMY is safe and produces no behaviour change in any other component.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from trader.alerts import send_alert
from trader.config import Config
from trader.data.alpaca_bars import get_daily_bars
from trader.data.crypto_bars import get_crypto_bars
from trader.execution.broker import AlpacaBroker, client_order_id_for
from trader.overlay import apply_overlay
from trader.portfolio.repository import (
    PROPOSAL_PENDING,
    OrderRow,
    ProposalRow,
    SignalRow,
    PortfolioRepository,
)
from trader.risk.gate import KillSwitch, OrderIntent, RiskDecision, RiskGate, is_crypto_symbol

if TYPE_CHECKING:
    from trader.strategy.base import PairSignal, PairStrategy, Signal, Strategy

logger = logging.getLogger(__name__)

# Rolling history window fed to each strategy. 200 days gives SMA(200) a full warm-up.
_BARS_LOOKBACK_DAYS = 200


@dataclass
class PipelineRun:
    run_id: int
    symbol: str
    signal: "Signal | None"
    risk_decision: RiskDecision
    outcome: Literal["executed", "queued", "blocked", "hold"]
    proposal_id: int | None = None
    order_id: str | None = None
    error: str | None = None


def run_pipeline(
    config: Config,
    strategies: "list[Strategy]",
    broker: AlpacaBroker,
    repo: PortfolioRepository,
    asof: datetime | None = None,
) -> list[PipelineRun]:
    """Run one pipeline tick across all strategies.

    `asof` defaults to now (UTC). Pass an explicit timestamp in tests or backfill runs
    to control what data is visible (no-lookahead guarantee from the Strategy base class).

    Reconciliation happens once per tick and is shared across all symbols. A stale
    reconciliation causes all symbols to block (fail-closed).
    """
    asof = asof or datetime.now(timezone.utc)
    gate = RiskGate(config.risk)
    kill_switch = KillSwitch(config.kill_switch_path)

    state = broker.reconcile()

    logger.info(
        "tick equity=%.2f trades_today=%d autonomy=%s",
        state.equity, state.trades_today, config.autonomy,
    )

    # Alert at most once per calendar day if the daily-loss breaker is live.
    _today = asof.date()
    if (
        state.daily_pnl_pct is not None
        and state.daily_pnl_pct <= -config.risk.daily_loss_limit_pct
        and getattr(run_pipeline, "_loss_alert_date", None) != _today
    ):
        send_alert(
            f"Daily-loss breaker tripped: {state.daily_pnl_pct:.2%} "
            f"(limit {-config.risk.daily_loss_limit_pct:.2%})",
            config.slack_webhook_url,
        )
        run_pipeline._loss_alert_date = _today  # type: ignore[attr-defined]

    results: list[PipelineRun] = []
    for strategy in strategies:
        result = _run_symbol(
            config=config,
            strategy=strategy,
            broker=broker,
            repo=repo,
            gate=gate,
            kill_switch=kill_switch,
            state=state,
            asof=asof,
        )
        results.append(result)
        logger.info(
            "pipeline symbol=%s outcome=%s reason=%s",
            result.symbol,
            result.outcome,
            result.risk_decision.reason,
        )
        # Update working state so subsequent symbols see an accurate picture within
        # this tick (avoids the shared-snapshot bug where two strategies evaluate
        # against the same pre-trade account state).
        if result.outcome in ("executed", "queued") and result.risk_decision.approved:
            from dataclasses import replace as _replace
            from trader.risk.gate import AccountState as _AS
            new_trades = state.trades_today + 1
            new_open = state.open_order_symbols | {result.symbol}
            approved_notional = result.risk_decision.approved_notional or 0.0
            new_headroom = max(state.equity * config.risk.max_position_pct - approved_notional, 0.0)
            # Reflect the trade in a new AccountState for the next iteration.
            state = _replace(
                state,
                trades_today=new_trades,
                open_order_symbols=new_open,
            )

    return results


def _run_symbol(
    *,
    config,
    strategy,
    broker,
    repo,
    gate,
    kill_switch,
    state,
    asof,
):
    from trader.risk.gate import AccountState

    symbol = strategy.symbol
    run_id = repo.record_run(strategy=type(strategy).__name__, mode=config.autonomy)

    signal = None
    risk_decision = RiskDecision.reject("not evaluated")

    try:
        # 1. Fetch bars (rolling window ending at asof).
        end = asof
        start = end - timedelta(days=_BARS_LOOKBACK_DAYS)
        bars = _fetch_bars(symbol, start, end, config)

        # 2. Stop-loss override — exit position immediately if down beyond threshold.
        import pandas as pd
        current_price = float(bars["close"].iloc[-1])
        entry_price = state.avg_entry_prices.get(symbol, 0.0)
        if (
            entry_price > 0
            and symbol in state.positions
            and state.positions[symbol] > 0
            and (current_price - entry_price) / entry_price <= -config.risk.stop_loss_pct
        ):
            signal = Signal(
                symbol,
                "sell",
                1.0,
                f"stop-loss: price {current_price:.4f} down "
                f"{(current_price - entry_price) / entry_price:.1%} from entry {entry_price:.4f}",
            )
            logger.warning(
                "stop-loss triggered symbol=%s entry=%.4f current=%.4f drawdown=%.1f%%",
                symbol, entry_price, current_price,
                (current_price - entry_price) / entry_price * 100,
            )
        else:
            # 3. Generate signal from strategy.
            signal = strategy.generate(bars, pd.Timestamp(asof))

        # 4. Hold signals skip the overlay and gate entirely.
        if signal.side == "hold":
            repo.record_signal(SignalRow(
                run_id=run_id,
                symbol=symbol,
                side=signal.side,
                strength=signal.strength,
                reason=signal.reason,
            ))
            return PipelineRun(
                run_id=run_id,
                symbol=symbol,
                signal=signal,
                risk_decision=RiskDecision.reject("hold signal — no order"),
                outcome="hold",
            )

        # 5. Sell with no open position will always be blocked by the gate — skip overlay.
        if signal.side == "sell" and not state.stale and symbol not in state.positions:
            repo.record_signal(SignalRow(
                run_id=run_id,
                symbol=symbol,
                side=signal.side,
                strength=signal.strength,
                reason=signal.reason,
            ))
            return PipelineRun(
                run_id=run_id,
                symbol=symbol,
                signal=signal,
                risk_decision=RiskDecision.reject("no position to sell"),
                outcome="blocked",
            )

        # 6. Overlay (Phase 6 — Claude LLM review, non-load-bearing). Only runs on buy/sell.
        # Stop-loss exits bypass the overlay — forced exit, no LLM deliberation needed.
        is_stop_loss = signal.reason.startswith("stop-loss:")
        if not is_stop_loss:
            signal = apply_overlay(signal, bars, config)

        # Record the post-overlay signal so the stored row matches what is used downstream.
        repo.record_signal(SignalRow(
            run_id=run_id,
            symbol=symbol,
            side=signal.side,
            strength=signal.strength,
            reason=signal.reason,
        ))

        # 7. Fail closed on stale state before building intent (equity may be zero).
        if state.stale:
            return PipelineRun(
                run_id=run_id,
                symbol=symbol,
                signal=signal,
                risk_decision=RiskDecision.reject("account state stale (reconciliation failed)"),
                outcome="blocked",
            )

        # 8. Build order intent.
        ref_price = float(bars["close"].iloc[-1])
        notional = _notional_for(signal, state, config, ref_price)
        intent = OrderIntent(
            symbol=symbol,
            side=signal.side,
            notional=notional,
            ref_price=ref_price,
            reason=signal.reason,
        )

        # 9. Risk gate.
        risk_decision = gate.evaluate(intent, state, kill_switch)
        logger.info(
            "gate symbol=%s approved=%s reason=%s approved_notional=%.2f",
            symbol, risk_decision.approved, risk_decision.reason,
            risk_decision.approved_notional,
        )
        if not risk_decision.approved:
            return PipelineRun(
                run_id=run_id,
                symbol=symbol,
                signal=signal,
                risk_decision=risk_decision,
                outcome="blocked",
            )

        # 10. Decision gate.
        if config.autonomy == "manual":
            proposal_id = repo.create_proposal(ProposalRow(
                symbol=symbol,
                side=signal.side,
                notional=risk_decision.approved_notional,
                ref_price=ref_price,
                reason=signal.reason,
            ))
            return PipelineRun(
                run_id=run_id,
                symbol=symbol,
                signal=signal,
                risk_decision=risk_decision,
                outcome="queued",
                proposal_id=proposal_id,
            )

        # autonomy == "auto"
        today = asof.date() if isinstance(asof, datetime) else asof
        client_order_id = client_order_id_for(
            today, symbol, signal.side, type(strategy).__name__
        )
        qty = state.positions.get(symbol, 0.0) if signal.side == "sell" else None
        order = broker.submit(
            symbol=symbol,
            side=signal.side,
            client_order_id=client_order_id,
            notional=risk_decision.approved_notional if signal.side == "buy" else None,
            qty=qty if signal.side == "sell" else None,
        )
        broker_order_id = str(getattr(order, "id", "") or "")
        repo.record_order(OrderRow(
            client_order_id=client_order_id,
            symbol=symbol,
            side=signal.side,
            notional=risk_decision.approved_notional,
            status="submitted",
            broker_order_id=broker_order_id or None,
        ))
        _env = "paper" if config.alpaca_paper else "LIVE"
        send_alert(
            f"FILL {symbol} {signal.side.upper()} ${risk_decision.approved_notional:.0f} {_env}",
            config.slack_webhook_url,
        )
        return PipelineRun(
            run_id=run_id,
            symbol=symbol,
            signal=signal,
            risk_decision=risk_decision,
            outcome="executed",
            order_id=client_order_id,
        )

    except Exception:
        logger.exception("pipeline error for %s", symbol)
        return PipelineRun(
            run_id=run_id,
            symbol=symbol,
            signal=signal,
            risk_decision=RiskDecision.reject("pipeline exception"),
            outcome="blocked",
            error="exception — see logs",
        )


def _fetch_bars(symbol: str, start: datetime, end: datetime, config: Config):
    """Route bar fetching by asset type."""
    if is_crypto_symbol(symbol):
        return get_crypto_bars(symbol, start=start, end=end, config=config)
    return get_daily_bars(symbol, start=start, end=end, config=config)


def run_pair_pipeline(
    config: Config,
    pair_strategies: "list[PairStrategy]",
    broker: AlpacaBroker,
    repo: PortfolioRepository,
    asof: datetime | None = None,
) -> list[PipelineRun]:
    """Run one pipeline tick for pairs/stat-arb strategies.

    Both legs of each pair are checked atomically: if either fails the risk gate,
    BOTH are blocked. This prevents naked single-leg exposure on partial fill.
    """
    asof = asof or datetime.now(timezone.utc)
    gate = RiskGate(config.risk)
    kill_switch = KillSwitch(config.kill_switch_path)
    state = broker.reconcile()

    results: list[PipelineRun] = []
    for strategy in pair_strategies:
        result_a, result_b = _run_pair(
            config=config,
            strategy=strategy,
            broker=broker,
            repo=repo,
            gate=gate,
            kill_switch=kill_switch,
            state=state,
            asof=asof,
        )
        results.extend([result_a, result_b])
        logger.info(
            "pair pipeline %s outcome_a=%s outcome_b=%s",
            strategy.symbol,
            result_a.outcome,
            result_b.outcome,
        )
    return results


def _run_pair(
    *,
    config,
    strategy,
    broker,
    repo,
    gate,
    kill_switch,
    state,
    asof,
):
    """Process one PairStrategy tick. Returns two PipelineRun results (one per leg)."""
    from trader.risk.gate import AccountState
    from trader.strategy.base import PairSignal

    sym_a = strategy.symbol_a
    sym_b = strategy.symbol_b
    run_id = repo.record_run(strategy=type(strategy).__name__, mode=config.autonomy)

    _blocked_a = PipelineRun(
        run_id=run_id, symbol=sym_a, signal=None,
        risk_decision=RiskDecision.reject("pair leg blocked"), outcome="blocked",
    )
    _blocked_b = PipelineRun(
        run_id=run_id, symbol=sym_b, signal=None,
        risk_decision=RiskDecision.reject("pair leg blocked"), outcome="blocked",
    )

    try:
        end = asof
        start = end - timedelta(days=_BARS_LOOKBACK_DAYS)
        bars_a = _fetch_bars(sym_a, start, end, config)
        bars_b = _fetch_bars(sym_b, start, end, config)

        import pandas as pd
        pair_signal = strategy.generate_pair(bars_a, bars_b, pd.Timestamp(asof))

        if pair_signal.is_hold:
            hold_decision = RiskDecision.reject("hold signal — no order")
            return (
                PipelineRun(run_id=run_id, symbol=sym_a, signal=None,
                            risk_decision=hold_decision, outcome="hold"),
                PipelineRun(run_id=run_id, symbol=sym_b, signal=None,
                            risk_decision=hold_decision, outcome="hold"),
            )

        if state.stale:
            return (
                PipelineRun(run_id=run_id, symbol=sym_a, signal=None,
                            risk_decision=RiskDecision.reject("account state stale"),
                            outcome="blocked"),
                PipelineRun(run_id=run_id, symbol=sym_b, signal=None,
                            risk_decision=RiskDecision.reject("account state stale"),
                            outcome="blocked"),
            )

        price_a = float(bars_a["close"].iloc[-1])
        price_b = float(bars_b["close"].iloc[-1])
        notional_a = _notional_for_side(pair_signal.side_a, sym_a, state, config, price_a)
        notional_b = _notional_for_side(pair_signal.side_b, sym_b, state, config, price_b)

        intent_a = OrderIntent(sym_a, pair_signal.side_a, notional_a, price_a, pair_signal.reason)
        intent_b = OrderIntent(sym_b, pair_signal.side_b, notional_b, price_b, pair_signal.reason)

        decision_a = gate.evaluate(intent_a, state, kill_switch)
        decision_b = gate.evaluate(intent_b, state, kill_switch)

        if not (decision_a.approved and decision_b.approved):
            combined_reason = (
                f"pair blocked: {sym_a}={decision_a.reason}, {sym_b}={decision_b.reason}"
            )
            return (
                PipelineRun(run_id=run_id, symbol=sym_a, signal=None,
                            risk_decision=RiskDecision.reject(combined_reason), outcome="blocked"),
                PipelineRun(run_id=run_id, symbol=sym_b, signal=None,
                            risk_decision=RiskDecision.reject(combined_reason), outcome="blocked"),
            )

        if config.autonomy == "manual":
            pid_a = repo.create_proposal(ProposalRow(
                symbol=sym_a, side=pair_signal.side_a,
                notional=decision_a.approved_notional, ref_price=price_a,
                reason=pair_signal.reason,
            ))
            pid_b = repo.create_proposal(ProposalRow(
                symbol=sym_b, side=pair_signal.side_b,
                notional=decision_b.approved_notional, ref_price=price_b,
                reason=pair_signal.reason,
            ))
            return (
                PipelineRun(run_id=run_id, symbol=sym_a, signal=None,
                            risk_decision=decision_a, outcome="queued", proposal_id=pid_a),
                PipelineRun(run_id=run_id, symbol=sym_b, signal=None,
                            risk_decision=decision_b, outcome="queued", proposal_id=pid_b),
            )

        today = asof.date() if isinstance(asof, datetime) else asof
        oid_a = client_order_id_for(today, sym_a, pair_signal.side_a, type(strategy).__name__)
        oid_b = client_order_id_for(today, sym_b, pair_signal.side_b, type(strategy).__name__)

        qty_a = state.positions.get(sym_a, 0.0) if pair_signal.side_a == "sell" else None
        qty_b = state.positions.get(sym_b, 0.0) if pair_signal.side_b == "sell" else None

        order_a = broker.submit(
            symbol=sym_a, side=pair_signal.side_a, client_order_id=oid_a,
            notional=decision_a.approved_notional if pair_signal.side_a == "buy" else None,
            qty=qty_a,
        )
        order_b = broker.submit(
            symbol=sym_b, side=pair_signal.side_b, client_order_id=oid_b,
            notional=decision_b.approved_notional if pair_signal.side_b == "buy" else None,
            qty=qty_b,
        )

        for oid, sym, side, decision in (
            (oid_a, sym_a, pair_signal.side_a, decision_a),
            (oid_b, sym_b, pair_signal.side_b, decision_b),
        ):
            repo.record_order(OrderRow(
                client_order_id=oid, symbol=sym, side=side,
                notional=decision.approved_notional, status="submitted",
                broker_order_id=str(getattr(
                    order_a if sym == sym_a else order_b, "id", ""
                ) or "") or None,
            ))

        return (
            PipelineRun(run_id=run_id, symbol=sym_a, signal=None,
                        risk_decision=decision_a, outcome="executed", order_id=oid_a),
            PipelineRun(run_id=run_id, symbol=sym_b, signal=None,
                        risk_decision=decision_b, outcome="executed", order_id=oid_b),
        )

    except Exception:
        logger.exception("pair pipeline error for %s", strategy.symbol)
        return (
            PipelineRun(run_id=run_id, symbol=sym_a, signal=None,
                        risk_decision=RiskDecision.reject("pipeline exception"),
                        outcome="blocked", error="exception — see logs"),
            PipelineRun(run_id=run_id, symbol=sym_b, signal=None,
                        risk_decision=RiskDecision.reject("pipeline exception"),
                        outcome="blocked", error="exception — see logs"),
        )


def _notional_for_side(side: str, symbol: str, state, config, ref_price: float) -> float:
    if side == "sell":
        held = state.positions.get(symbol, 0.0)
        return max(held * ref_price, 1.0)
    return config.risk.max_crypto_position_pct * state.equity


def _notional_for(signal, state, config, ref_price: float) -> float:
    """Compute the intended notional for an order.

    Buys: target full position cap (gate will size down if needed).
    Sells: use held value (gate enforces long/flat constraint).
    """
    if signal.side == "sell":
        held = state.positions.get(signal.symbol, 0.0)
        return max(held * ref_price, 1.0)
    # buy: target the max position size so the gate can size down to headroom
    return config.risk.max_position_pct * state.equity
