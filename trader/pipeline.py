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
from trader.data.alpaca_bars import get_daily_bars, get_daily_bars_batch
from trader.data.crypto_bars import get_crypto_bars
from trader.execution.broker import AlpacaBroker, client_order_id_for
from trader.overlay import apply_fundamental_gate, apply_overlay
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

    # Seed ownership from DB; prune entries for positions no longer held and for
    # owner strategies no longer in the active stack — otherwise a position bought
    # by a retired strategy could only ever exit via stop-loss.
    try:
        active_strategy_names = {type(s).__name__ for s in strategies}
        loaded_owners = repo.get_position_owners()
        loaded_owners = {
            s: o for s, o in loaded_owners.items()
            if s in state.positions and o in active_strategy_names
        }
        if loaded_owners:
            from dataclasses import replace as _replace_init
            state = _replace_init(state, position_owners=loaded_owners)
    except Exception:
        logger.warning("failed to load position owners from DB — starting with empty ownership")

    logger.info(
        "tick equity=%.2f trades_today=%d autonomy=%s",
        state.equity, state.trades_today, config.autonomy,
    )

    # DISABLED: daily-loss breaker alert — breaker itself disabled for performance monitoring.
    # _today = asof.date()
    # if (
    #     state.daily_pnl_pct is not None
    #     and state.daily_pnl_pct <= -config.risk.daily_loss_limit_pct
    #     and getattr(run_pipeline, "_loss_alert_date", None) != _today
    # ):
    #     send_alert(
    #         f"Daily-loss breaker tripped: {state.daily_pnl_pct:.2%} "
    #         f"(limit {-config.risk.daily_loss_limit_pct:.2%})",
    #         config.slack_webhook_url,
    #         alert_email=config.alert_email,
    #         smtp_user=config.smtp_user,
    #         smtp_password=config.smtp_password,
    #     )
    #     run_pipeline._loss_alert_date = _today  # type: ignore[attr-defined]

    # Pre-fetch bars for all equity symbols in one batch call.
    # Crypto symbols are excluded — they use a separate data path.
    equity_symbols = list({
        s.symbol for s in strategies if not is_crypto_symbol(s.symbol)
    })
    if equity_symbols:
        end = asof
        start = end - timedelta(days=_BARS_LOOKBACK_DAYS)
        bars_cache: dict[str, object] = get_daily_bars_batch(equity_symbols, start, end, config)
    else:
        bars_cache = {}

    results: list[PipelineRun] = []
    pending_buys: list[tuple] = []  # (strength, strategy, signal, bars, run_id)

    # Phase 1: generate signals; execute sells immediately, stash buys for ranking.
    for strategy in strategies:
        prep = _prepare_signal(
            config=config,
            strategy=strategy,
            repo=repo,
            state=state,
            asof=asof,
            bars_cache=bars_cache,
        )
        if prep is None:
            continue
        if isinstance(prep, PipelineRun):
            results.append(prep)
            logger.info(
                "pipeline symbol=%s outcome=%s reason=%s",
                prep.symbol, prep.outcome, prep.risk_decision.reason,
            )
            continue
        signal, bars, run_id = prep
        if signal.side == "sell":
            result = _execute_signal(
                signal=signal, bars=bars, run_id=run_id, strategy=strategy,
                config=config, broker=broker, repo=repo, gate=gate,
                kill_switch=kill_switch, state=state, asof=asof,
            )
            results.append(result)
            logger.info(
                "pipeline symbol=%s outcome=%s reason=%s",
                result.symbol, result.outcome, result.risk_decision.reason,
            )
            if result.outcome in ("executed", "queued") and result.risk_decision.approved:
                state = _advance_state(state, result, strategy, repo)
        else:
            pending_buys.append((signal.strength, strategy, signal, bars, run_id))

    # Phase 2: rank buys by signal strength (highest conviction first), execute in order.
    # Each buy sizes off remaining free cash, so the best signal gets the most capital.
    pending_buys.sort(key=lambda x: x[0], reverse=True)
    for _, strategy, signal, bars, run_id in pending_buys:
        result = _execute_signal(
            signal=signal, bars=bars, run_id=run_id, strategy=strategy,
            config=config, broker=broker, repo=repo, gate=gate,
            kill_switch=kill_switch, state=state, asof=asof,
        )
        results.append(result)
        logger.info(
            "pipeline symbol=%s outcome=%s reason=%s",
            result.symbol, result.outcome, result.risk_decision.reason,
        )
        if result.outcome in ("executed", "queued") and result.risk_decision.approved:
            state = _advance_state(state, result, strategy, repo)

    return results


def _advance_state(state, result, strategy, repo):
    """Return updated AccountState after an approved trade within a tick."""
    from dataclasses import replace as _replace
    approved_notional = result.risk_decision.approved_notional or 0.0
    new_owners = dict(state.position_owners)
    if result.signal is not None:
        if result.signal.side == "buy" and result.symbol not in new_owners:
            new_owners[result.symbol] = type(strategy).__name__
            try:
                repo.set_position_owner(result.symbol, type(strategy).__name__)
            except Exception:
                logger.warning("failed to persist owner for %s", result.symbol)
        elif result.signal.side == "sell":
            new_owners.pop(result.symbol, None)
            try:
                repo.clear_position_owner(result.symbol)
            except Exception:
                logger.warning("failed to clear owner for %s", result.symbol)
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


def _prepare_signal(
    *,
    config,
    strategy,
    repo,
    state,
    asof,
    bars_cache: dict | None = None,
):
    """Generate and pre-screen a signal for a strategy.

    Returns None if bars are unavailable.
    Returns PipelineRun for terminal cases (hold, blocked, vetoed).
    Returns (signal, bars, run_id) when the signal is ready for gate evaluation.
    """
    from trader.strategy.base import Signal

    symbol = strategy.symbol
    run_id = repo.record_run(strategy=type(strategy).__name__, mode=config.autonomy)
    signal = None

    try:
        import pandas as pd
        end = asof
        start = end - timedelta(days=_BARS_LOOKBACK_DAYS)
        bars = _fetch_bars(symbol, start, end, config, cache=bars_cache)

        if bars.empty:
            logger.warning("no bar data for %s — skipping stop-loss and signal", symbol)
            return None

        current_price = float(bars["close"].iloc[-1])
        entry_price = state.avg_entry_prices.get(symbol, 0.0)
        if (
            entry_price > 0
            and symbol in state.positions
            and state.positions[symbol] > 0
            and (current_price - entry_price) / entry_price <= -(
                config.risk.crypto_stop_loss_pct if is_crypto_symbol(symbol) else config.risk.stop_loss_pct
            )
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
            signal = strategy.generate(bars, pd.Timestamp(asof))

        if signal.side == "hold":
            repo.record_signal(SignalRow(
                run_id=run_id, symbol=symbol,
                side=signal.side, strength=signal.strength, reason=signal.reason,
            ))
            return PipelineRun(
                run_id=run_id, symbol=symbol, signal=signal,
                risk_decision=RiskDecision.reject("hold signal — no order"), outcome="hold",
            )

        if signal.side == "sell" and not state.stale and symbol not in state.positions:
            repo.record_signal(SignalRow(
                run_id=run_id, symbol=symbol,
                side=signal.side, strength=signal.strength, reason=signal.reason,
            ))
            return PipelineRun(
                run_id=run_id, symbol=symbol, signal=signal,
                risk_decision=RiskDecision.reject("no position to sell"), outcome="blocked",
            )

        if signal.side == "sell" and not signal.reason.startswith("stop-loss:"):
            owner = state.position_owners.get(symbol)
            if owner is not None and owner != type(strategy).__name__:
                repo.record_signal(SignalRow(
                    run_id=run_id, symbol=symbol,
                    side=signal.side, strength=signal.strength, reason=signal.reason,
                ))
                return PipelineRun(
                    run_id=run_id, symbol=symbol, signal=signal,
                    risk_decision=RiskDecision.reject(
                        f"ownership conflict: {symbol} owned by {owner}, "
                        f"not {type(strategy).__name__}"
                    ),
                    outcome="blocked",
                )

        is_first_entry = symbol not in state.positions or state.positions.get(symbol, 0.0) == 0.0
        if signal.side == "buy" and is_first_entry and not is_crypto_symbol(symbol):
            date_str = asof.strftime("%Y-%m-%d")
            if not apply_fundamental_gate(symbol, bars, config, date_str):
                veto_signal = Signal(
                    symbol, "hold", 0.0,
                    "[fundamental gate veto] financials/trend failed quality check",
                )
                repo.record_signal(SignalRow(
                    run_id=run_id, symbol=symbol,
                    side=veto_signal.side, strength=veto_signal.strength, reason=veto_signal.reason,
                ))
                return PipelineRun(
                    run_id=run_id, symbol=symbol, signal=veto_signal,
                    risk_decision=RiskDecision.reject("fundamental gate veto"), outcome="hold",
                )

        if not signal.reason.startswith("stop-loss:"):
            signal = apply_overlay(signal, bars, config)

        repo.record_signal(SignalRow(
            run_id=run_id, symbol=symbol,
            side=signal.side, strength=signal.strength, reason=signal.reason,
        ))

        if signal.side == "hold":
            return PipelineRun(
                run_id=run_id, symbol=symbol, signal=signal,
                risk_decision=RiskDecision.reject("overlay veto"), outcome="hold",
            )

        return signal, bars, run_id

    except Exception:
        logger.exception("pipeline error for %s", symbol)
        return PipelineRun(
            run_id=run_id, symbol=symbol, signal=signal,
            risk_decision=RiskDecision.reject("pipeline exception"),
            outcome="blocked", error="exception — see logs",
        )


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
):
    """Gate evaluation and order submission for a pre-generated signal."""
    symbol = signal.symbol

    try:
        if state.stale:
            return PipelineRun(
                run_id=run_id, symbol=symbol, signal=signal,
                risk_decision=RiskDecision.reject("account state stale (reconciliation failed)"),
                outcome="blocked",
            )

        ref_price = float(bars["close"].iloc[-1])
        notional = _notional_for(signal, state, config, ref_price)
        intent = OrderIntent(
            symbol=symbol, side=signal.side,
            notional=notional, ref_price=ref_price, reason=signal.reason,
        )

        risk_decision = gate.evaluate(intent, state, kill_switch)
        logger.info(
            "gate symbol=%s approved=%s reason=%s approved_notional=%.2f",
            symbol, risk_decision.approved, risk_decision.reason,
            risk_decision.approved_notional,
        )
        if not risk_decision.approved:
            return PipelineRun(
                run_id=run_id, symbol=symbol, signal=signal,
                risk_decision=risk_decision, outcome="blocked",
            )

        if config.autonomy == "manual":
            proposal_id = repo.create_proposal(ProposalRow(
                symbol=symbol, side=signal.side,
                notional=risk_decision.approved_notional,
                ref_price=ref_price, reason=signal.reason,
            ))
            return PipelineRun(
                run_id=run_id, symbol=symbol, signal=signal,
                risk_decision=risk_decision, outcome="queued", proposal_id=proposal_id,
            )

        today = asof.date() if isinstance(asof, datetime) else asof
        client_order_id = client_order_id_for(
            today, symbol, signal.side, type(strategy).__name__
        )
        qty = state.positions.get(symbol, 0.0) if signal.side == "sell" else None
        order = broker.submit(
            symbol=symbol, side=signal.side,
            client_order_id=client_order_id,
            notional=risk_decision.approved_notional if signal.side == "buy" else None,
            qty=qty if signal.side == "sell" else None,
            ref_price=ref_price,
        )
        broker_order_id = str(getattr(order, "id", "") or "")
        repo.record_order(OrderRow(
            client_order_id=client_order_id, symbol=symbol,
            side=signal.side, notional=risk_decision.approved_notional,
            status="submitted", broker_order_id=broker_order_id or None,
        ))
        _env = "paper" if config.alpaca_paper else "LIVE"
        send_alert(
            f"FILL {symbol} {signal.side.upper()} ${risk_decision.approved_notional:.0f} {_env}",
            config.slack_webhook_url,
            alert_email=config.alert_email,
            smtp_user=config.smtp_user,
            smtp_password=config.smtp_password,
        )
        return PipelineRun(
            run_id=run_id, symbol=symbol, signal=signal,
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


def _fetch_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    config: Config,
    cache: dict | None = None,
):
    """Route bar fetching by asset type. Uses pre-fetched cache when available."""
    if cache and symbol in cache:
        cached = cache[symbol]
        if cached is not None and not cached.empty:
            return cached
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
            ref_price=price_a,
        )
        order_b = broker.submit(
            symbol=sym_b, side=pair_signal.side_b, client_order_id=oid_b,
            notional=decision_b.approved_notional if pair_signal.side_b == "buy" else None,
            qty=qty_b,
            ref_price=price_b,
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

    Buys: take cap_pct of remaining investable cash (cash already committed this
    tick is subtracted so each successive buy gets a smaller slice — geometric decay).
    Sells: use held value (gate enforces long/flat constraint).
    """
    if signal.side == "sell":
        held = state.positions.get(signal.symbol, 0.0)
        return max(held * ref_price, 1.0)
    cap_pct = (
        config.risk.max_crypto_position_pct
        if is_crypto_symbol(signal.symbol)
        else config.risk.max_position_pct
    )
    free_cash = max(state.cash - state.deployed_notional, 0.0)
    return cap_pct * free_cash
