"""The pipeline spine: tick → data → strategy → overlay → risk gate → decision gate → execute/queue → record.

The decision gate is the ONLY difference between AUTONOMY=manual and AUTONOMY=auto:
  manual  → risk-approved orders become proposals in the repo queue
  auto    → risk-approved orders execute directly via the broker

Both paths share the same risk gate, kill switch, and broker adapter, so flipping
AUTONOMY is safe and produces no behaviour change in any other component.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

_ALPACA_MIN_ORDER = 10.0
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from trader.alerts import send_alert
from trader.config import Config
from trader.data.alpaca_bars import get_daily_bars, get_daily_bars_batch, get_live_prices_batch, get_intraday_bars_batch
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
from trader.risk.vol_sizing import vol_scale
from trader.strategy.regime import classify_regime

if TYPE_CHECKING:
    from trader.strategy.base import PairSignal, PairStrategy, Signal, Strategy

logger = logging.getLogger(__name__)

# Rolling history window fed to each strategy. 200 days gives SMA(200) a full warm-up.
_BARS_LOOKBACK_DAYS = 200

# How many minutes before 4 PM ET to force-exit intraday positions.
_EOD_EXIT_MINUTES = int(os.getenv("EOD_EXIT_MINUTES", "15"))

# Signal cache populated by precompute_signals() after market close.
# Keyed by (strategy_class_name, symbol) → (cache_date, Signal).
# Market-open ticks read from here to avoid re-running strategy.generate() at the
# exact moment execution matters most. Crypto excluded (24/7, not daily-bar based).
_premarket_signals: dict[tuple[str, str], tuple] = {}


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
        loaded_owners = repo.get_position_owners()  # dict[tuple[str,str], str]
        loaded_owners = {
            key: o for key, o in loaded_owners.items()
            if key[0] in state.positions and o in active_strategy_names
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

    from trader.strategy.base import IntradayStrategy

    # Pre-fetch daily bars for all non-intraday equity symbols in one batch call.
    # Crypto and intraday symbols are excluded — they use separate data paths.
    equity_symbols = list({
        s.symbol for s in strategies
        if not is_crypto_symbol(s.symbol) and not isinstance(s, IntradayStrategy)
    })
    live_prices: dict[str, float] = {}
    live_spread_pcts: dict[str, float] = {}
    if equity_symbols:
        end = asof
        start = end - timedelta(days=_BARS_LOOKBACK_DAYS)
        try:
            bars_cache: dict[str, object] = get_daily_bars_batch(equity_symbols, start, end, config)
        except Exception:
            logger.exception("equity bars batch fetch failed — all equity symbols skipped this tick")
            bars_cache = {}
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
    try:
        from trader.strategy.gap_and_go import GapAndGo
        for _s in strategies:
            if isinstance(_s, GapAndGo):
                _gap_sym = _s.symbol
                if _gap_sym not in bars_cache:
                    _gap_daily = get_daily_bars_batch([_gap_sym], start, end, config)
                    bars_cache.update(_gap_daily)
                if _gap_sym in bars_cache and not bars_cache[_gap_sym].empty:
                    _s.prev_close = float(bars_cache[_gap_sym]["close"].iloc[-1])
    except ImportError:
        pass

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

    results: list[PipelineRun] = []
    pending_buys: list[tuple] = []  # (strength, strategy, signal, bars, run_id, pool)

    # Phase 1: generate signals; execute sells immediately, stash buys for ranking.
    for strategy in strategies:
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
        if prep is None:
            continue
        if isinstance(prep, PipelineRun):
            results.append(prep)
            logger.info(
                "pipeline symbol=%s outcome=%s reason=%s",
                prep.symbol, prep.outcome, prep.risk_decision.reason,
            )
            continue
        signal, bars, run_id, pool = prep
        if signal.side == "sell":
            result = _execute_signal(
                signal=signal, bars=bars, run_id=run_id, strategy=strategy,
                config=config, broker=broker, repo=repo, gate=gate,
                kill_switch=kill_switch, state=state, asof=asof,
                live_prices=live_prices, live_spread_pcts=live_spread_pcts,
                pool=pool,
            )
            results.append(result)
            logger.info(
                "pipeline symbol=%s outcome=%s reason=%s",
                result.symbol, result.outcome, result.risk_decision.reason,
            )
            if result.outcome in ("executed", "queued") and result.risk_decision.approved:
                state = _advance_state(state, result, strategy, repo)
        else:
            pending_buys.append((signal.strength, strategy, signal, bars, run_id, pool))

    # Phase 2: rank buys by signal strength; bandit weighting adjusts ranking when enabled.
    if config.risk.bandit_weighting_shadow or config.risk.bandit_weighting_live:
        _bandit_w = repo.get_all_bandit_weights()

        def _rank_key(item):
            raw, strat, sig, bars, _, pool = item
            regime = classify_regime(bars)
            arm = (type(strat).__name__, regime)
            w, _ = _bandit_w.get(arm, (1.0, 0))
            effective = raw * w
            if config.risk.bandit_weighting_live:
                return effective
            logger.info(
                "bandit shadow arm=%s effective=%.4f raw=%.4f weight=%.4f",
                arm, effective, raw, w,
            )
            return raw

        pending_buys.sort(key=_rank_key, reverse=True)
    else:
        pending_buys.sort(key=lambda x: x[0], reverse=True)
    for _, strategy, signal, bars, run_id, pool in pending_buys:
        corr_factor = _correlation_factor(signal.symbol, state, bars_cache)
        result = _execute_signal(
            signal=signal, bars=bars, run_id=run_id, strategy=strategy,
            config=config, broker=broker, repo=repo, gate=gate,
            kill_switch=kill_switch, state=state, asof=asof,
            live_prices=live_prices, live_spread_pcts=live_spread_pcts,
            corr_factor=corr_factor, pool=pool,
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


def precompute_signals(
    config: Config,
    strategies: list,
    asof: datetime,
    bars_cache: dict | None = None,
) -> int:
    """Compute and cache buy/sell signals for all equity strategies after market close.

    Safe to call from the scheduler's post-close tick. Returns count of signals cached.
    Crypto and intraday strategies are skipped — they run on live bars, not daily bars.
    """
    import pandas as pd
    from datetime import date as _date
    today = asof.date() if hasattr(asof, "date") else asof
    end = asof
    start = end - timedelta(days=_BARS_LOOKBACK_DAYS)
    cached = 0
    for strategy in strategies:
        symbol = strategy.symbol
        from trader.strategy.base import IntradayStrategy
        if is_crypto_symbol(symbol) or isinstance(strategy, IntradayStrategy):
            continue
        key = (type(strategy).__name__, symbol)
        try:
            bars = _fetch_bars(symbol, start, end, config, cache=bars_cache)
            if bars.empty:
                continue
            signal = strategy.generate(bars, pd.Timestamp(asof))
            _premarket_signals[key] = (today, signal)
            cached += 1
        except Exception:
            logger.warning(
                "precompute_signals failed for %s/%s", type(strategy).__name__, symbol,
                exc_info=True,
            )
    logger.info("pre-market signal precompute: %d signals cached for %s", cached, today)
    return cached


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
    """Generate and pre-screen a signal for a strategy.

    Returns None if bars are unavailable.
    Returns PipelineRun for terminal cases (hold, blocked, vetoed).
    Returns (signal, bars, run_id, pool) when the signal is ready for gate evaluation.
    """
    from trader.strategy.base import Signal

    symbol = strategy.symbol
    run_id = repo.record_run(strategy=type(strategy).__name__, mode=config.autonomy)
    signal = None

    try:
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

        if bars.empty:
            logger.warning("no bar data for %s — skipping stop-loss and signal", symbol)
            return None

        # On first tick after a cold start (process restart), reconstruct any
        # stateful exit tracking from bar history for symbols we still hold.
        # Only warm up as entered if this strategy owns the position; otherwise
        # mark warmed-up so non-owning strategies don't falsely set _entered.
        if not strategy._warmed_up:
            if symbol in state.positions:
                _warm_owner = state.position_owners.get((symbol, _pool))
                if _warm_owner is None or _warm_owner == type(strategy).__name__:
                    strategy.warm_up(bars)
                else:
                    strategy._warmed_up = True
            else:
                strategy._warmed_up = True

        current_price = (live_prices or {}).get(symbol) or float(bars["close"].iloc[-1])
        entry_price = state.avg_entry_prices.get(symbol, 0.0)
        _stop_exempt = state.position_owners.get((symbol, _pool)) == "DipRecovery"
        if (
            not _stop_exempt
            and entry_price > 0
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
            # EOD exit: force-sell intraday positions 15 min before market close.
            # Only the owning strategy fires the exit to prevent duplicate sells when
            # multiple intraday strategies share a symbol.
            # Early-return so overlay/gate/ownership checks are bypassed — same as stop-loss path.
            if _is_intraday and strategy.eod_exit and symbol in state.positions and state.positions[symbol] > 0:
                _eod_owner = state.position_owners.get((symbol, "intraday"))
                if _eod_owner and _eod_owner != type(strategy).__name__:
                    pass  # not our position — skip EOD exit for this strategy
                else:
                    from zoneinfo import ZoneInfo as _ZI
                    from datetime import timezone as _timezone
                    _ny = _ZI("America/New_York")
                    _asof_ny = asof.astimezone(_ny) if asof.tzinfo else asof.replace(tzinfo=_timezone.utc).astimezone(_ny)
                    _close_ny = _asof_ny.replace(hour=16, minute=0, second=0, microsecond=0)
                    if _asof_ny >= _close_ny - timedelta(minutes=_EOD_EXIT_MINUTES):
                        signal = Signal(
                            symbol, "sell", 1.0,
                            f"eod-exit: intraday flat at {_asof_ny.strftime('%H:%M')} ET",
                        )
                        repo.record_signal(SignalRow(
                            run_id=run_id, symbol=symbol,
                            side=signal.side, strength=signal.strength, reason=signal.reason,
                        ))
                        return signal, bars, run_id, _pool

            _cache_key = (type(strategy).__name__, symbol)
            _today = asof.date() if hasattr(asof, "date") else asof
            _cached = _premarket_signals.get(_cache_key)
            if _cached is not None and _cached[0] == _today and not is_crypto_symbol(symbol):
                signal = _cached[1]
                logger.debug("using precomputed signal for %s/%s", type(strategy).__name__, symbol)
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

        if signal.side == "sell" and not signal.reason.startswith("stop-loss:") and not signal.reason.startswith("eod-exit:"):
            owner = state.position_owners.get((symbol, _pool))
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
        if signal.side == "buy" and is_first_entry and not is_crypto_symbol(symbol) and not _is_intraday:
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

        if not signal.reason.startswith("stop-loss:") and not signal.reason.startswith("eod-exit:") and not _is_intraday:
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

        return signal, bars, run_id, _pool

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
    live_prices: dict | None = None,
    live_spread_pcts: dict | None = None,
    corr_factor: float = 1.0,
    pool: str = "daily",
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

        ref_price = (live_prices or {}).get(signal.symbol) or float(bars["close"].iloc[-1])

        # For auto-mode sells: cancel broker-side stop before gate evaluation so the
        # open stop order doesn't appear in open_order_symbols and block the sell.
        if signal.side == "sell" and config.autonomy == "auto" and not is_crypto_symbol(symbol):
            broker.cancel_open_stops(symbol)
            from dataclasses import replace as _replace_for_gate
            state = _replace_for_gate(state, open_order_symbols=state.open_order_symbols - {symbol})

        notional = _notional_for(signal, state, config, ref_price, bars=bars, corr_factor=corr_factor, pool=pool)
        spread_pct = (live_spread_pcts or {}).get(signal.symbol, 0.0)
        intent = OrderIntent(
            symbol=symbol, side=signal.side,
            notional=notional, ref_price=ref_price, reason=signal.reason,
            spread_pct=spread_pct, pool=pool,
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
            strategy_name=type(strategy).__name__,
            regime=classify_regime(bars),
            signal_strength=signal.strength,
        ))

        # Place a broker-side GTC stop to protect new long positions.
        if signal.side == "buy" and not is_crypto_symbol(symbol):
            stop_price = ref_price * (1 - config.risk.stop_loss_pct)
            stop_qty = round(risk_decision.approved_notional / ref_price, 6)
            # Cap to currently-held shares so we never stop more than we own.
            # New positions (no state entry) keep the computed qty.
            held = state.positions.get(symbol, 0.0)
            if held > 0:
                stop_qty = min(stop_qty, held)
            stop_oid = client_order_id_for(
                today, symbol, "sell", f"stop-{type(strategy).__name__}"
            )
            try:
                broker.cancel_open_stops(symbol)
                broker.place_stop_order(
                    symbol=symbol, qty=stop_qty,
                    stop_price=stop_price, client_order_id=stop_oid,
                )
                logger.info("placed GTC stop for %s at %.2f", symbol, stop_price)
            except Exception:
                logger.exception("stop order failed for %s — software stop remains active", symbol)

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
    free_cash = max(state.cash - state.deployed_notional - config.risk.min_cash_reserve, 0.0)
    sized = config.risk.max_crypto_position_pct * free_cash
    return min(free_cash, max(sized, _ALPACA_MIN_ORDER))


def _correlation_factor(symbol: str, state, bars_cache: dict) -> float:
    """Return 0.5 if symbol has >0.7 rolling-60d correlation with a held position, else 1.0.

    Prevents double-concentration when two correlated names both get buy signals.
    Only fires when bars are available for both sides; defaults to 1.0 when unknown.
    """
    if symbol not in bars_cache:
        return 1.0
    held = [s for s in state.positions if s in bars_cache and state.positions.get(s, 0.0) > 0]
    if not held:
        return 1.0
    sym_ret = bars_cache[symbol]["close"].pct_change().dropna().tail(60)
    for h in held:
        h_ret = bars_cache[h]["close"].pct_change().dropna().tail(60)
        a, b = sym_ret.align(h_ret, join="inner")
        if len(a) >= 20 and a.corr(b) > 0.7:
            logger.info(
                "corr-aware sizing: %s ↔ %s corr>0.7 → halving size", symbol, h
            )
            return 0.5
    return 1.0


def _notional_for(signal, state, config, ref_price: float, bars=None, corr_factor: float = 1.0, pool: str = "daily") -> float:
    """Compute the intended notional for an order.

    Buys: take cap_pct of remaining investable cash (cash already committed this
    tick is subtracted so each successive buy gets a smaller slice — geometric decay),
    then scale by vol_scale() so high-volatility regimes deploy less capital.
    When the sized amount falls below Alpaca's minimum order, the full free_cash
    is used instead (capped at free_cash so we never over-commit).
    Sells: use held value (gate enforces long/flat constraint).

    Pool routing: intraday pool uses 40% of cash (intraday_pool_pct); daily pool uses
    the remaining 60% minus min_cash_reserve.

    Note: _notional_for_side() (pairs pipeline) is a separate function and is
    intentionally left without vol-scaling for now.
    """
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
    if bars is not None:
        scale = vol_scale(bars, annualization=365 if is_crypto else 252)
        logger.debug("vol_scale symbol=%s scale=%.3f", signal.symbol, scale)
    else:
        scale = 1.0
    sized = cap_pct * free_cash * scale * corr_factor
    return min(free_cash, max(sized, _ALPACA_MIN_ORDER))
