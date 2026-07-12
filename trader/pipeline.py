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
from trader.execution.options_broker import AlpacaOptionsBroker, options_client_order_id_for
from trader.overlay import apply_fundamental_gate, apply_overlay
from trader.portfolio.repository import (
    PROPOSAL_PENDING,
    OptionsPositionRow,
    OrderRow,
    ProposalRow,
    SignalRow,
    PortfolioRepository,
    TradeOutcomeRow,
)
from trader.risk.gate import (
    KillSwitch, OptionsOrderIntent, OrderIntent, RiskDecision, RiskGate,
    effective_autonomy, is_crypto_symbol,
)
from trader.risk.vol_sizing import vol_scale
from trader.strategy.dip_recovery import DipRecovery
from trader.strategy.regime import classify_regime
from trader.strategy.wheel import WheelStrategy

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
    is_options: bool = False  # True when this run opened a CSP/CC rather than a stock order
    # False when the order was submitted but its fill was not confirmed within the
    # wait_for_fill window. Unconfirmed sells must NOT clear position ownership —
    # reconcile_order_statuses settles them later against broker truth.
    fill_confirmed: bool = True


def run_pipeline(
    config: Config,
    strategies: "list[Strategy]",
    broker: AlpacaBroker,
    repo: PortfolioRepository,
    asof: datetime | None = None,
    options_broker: "AlpacaOptionsBroker | None" = None,
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

    if config.risk.options_trading_enabled:
        try:
            total_collateral = repo.get_total_options_collateral()
            from dataclasses import replace as _replace_collateral
            state = _replace_collateral(state, options_collateral=total_collateral)
        except Exception:
            # Fail closed: set options_collateral to the full equity value so the
            # combined-cap check in evaluate_options_order will reject any new options
            # order until the next successful reconciliation. Log the full traceback
            # so the root cause is surfaced rather than silently swallowed.
            logger.exception(
                "failed to load options collateral — setting sentinel to block new options orders"
            )
            from dataclasses import replace as _replace_collateral
            state = _replace_collateral(state, options_collateral=state.equity)

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

    # Fold recent losing exits into state for the risk gate's symbol-cooldown check.
    # Fail-open: a lookup failure just means "no cooldown data this tick", not a halt.
    try:
        recent_outcomes = repo.get_recent_outcomes(limit=200)
        last_losing_exit_at: dict = {}
        for o in recent_outcomes:
            if o["pnl_pct"] < 0 and o["symbol"] not in last_losing_exit_at:
                last_losing_exit_at[o["symbol"]] = datetime.fromisoformat(o["closed_at"])
        if last_losing_exit_at:
            from dataclasses import replace as _replace_cooldown
            state = _replace_cooldown(state, last_losing_exit_at=last_losing_exit_at)
    except Exception:
        logger.warning("failed to load trade outcomes from DB — cooldown check has no data this tick")

    logger.info(
        "tick equity=%.2f trades_today=%d autonomy=%s",
        state.equity, state.trades_today, effective_autonomy(config),
    )

    # Daily-loss breaker alert — fires once per day when the account's drawdown hits
    # the limit, so a halted book is visible instead of silent. The gate already
    # enforces the halt (RiskGate rejects new buys); this only notifies. Gated on the
    # same flag as the halt so monitoring-only deployments (halt off) stay quiet.
    if (
        config.risk.daily_loss_halt_enabled
        and state.daily_pnl_pct is not None
        and state.daily_pnl_pct <= -config.risk.daily_loss_limit_pct
        and getattr(run_pipeline, "_loss_alert_date", None) != asof.date()
    ):
        send_alert(
            f"Daily-loss breaker tripped: {state.daily_pnl_pct:.2%} "
            f"(limit {-config.risk.daily_loss_limit_pct:.2%}) — new buys halted",
            config.slack_webhook_url,
            alert_email=config.alert_email,
            smtp_user=config.smtp_user,
            smtp_password=config.smtp_password,
        )
        run_pipeline._loss_alert_date = asof.date()  # type: ignore[attr-defined]

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
                pool=pool, options_broker=options_broker,
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
            corr_factor=corr_factor, pool=pool, options_broker=options_broker,
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

    if result.is_options:
        # CSP-on-dip / Wheel opened a contract this tick — bump collateral so a later
        # signal in the same tick sees the updated combined-allocation headroom.
        # Also add the underlying to open_order_symbols so a second signal for the
        # same underlying within this tick is blocked, matching equity behaviour.
        return _replace(
            state,
            trades_today=state.trades_today + 1,
            open_order_symbols=state.open_order_symbols | {result.symbol},
            options_collateral=state.options_collateral + (result.risk_decision.approved_notional or 0.0),
        )

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
            if result.fill_confirmed:
                new_owners.pop(owner_key, None)
                try:
                    repo.clear_position_owner(result.symbol, pool)
                except Exception:
                    logger.warning("failed to clear owner for %s/%s", result.symbol, pool)
            else:
                # Sell submitted but fill unconfirmed — keep ownership so the owning
                # strategy can still manage the position if the order never fills.
                # reconcile_order_statuses clears the owner once the fill is verified.
                logger.warning(
                    "sell for %s/%s unconfirmed — retaining position owner",
                    result.symbol, pool,
                )
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


def reconcile_order_statuses(broker, repo, max_age_days: int = 3) -> int:
    """Settle repo orders stuck at 'submitted' against broker truth.

    The tick path only waits ~5s for a fill; anything slower stays 'submitted' in the
    audit trail forever, and an unconfirmed sell defers its trade outcome and owner
    clearing (see _advance_state). This job closes that loop: for each stuck order it
    asks the broker for the real status, upserts it, and for late-FILLED sells records
    the deferred outcome and clears position ownership. Returns rows updated.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    try:
        stuck = repo.get_orders_by_status("submitted", since)
    except Exception:
        logger.exception("order-status reconciliation: repo query failed")
        return 0
    if not stuck:
        return 0

    updated = 0
    for row in stuck:
        coid = row["client_order_id"]
        order = broker.get_order(coid)
        if order is None:
            continue  # lookup failed — status unknown, retry next pass
        status = str(getattr(order, "status", "")).lower()
        if status == "filled":
            new_status = "filled"
        elif status in {"canceled", "cancelled", "expired", "rejected"}:
            new_status = "canceled" if status == "cancelled" else status
        else:
            continue  # still live (new/accepted/partially_filled) — leave as submitted

        repo.record_order(OrderRow(
            client_order_id=coid, symbol=row["symbol"], side=row["side"],
            notional=row["notional"], status=new_status,
        ))
        updated += 1
        logger.info(
            "order reconciliation: %s %s %s -> %s",
            row["symbol"], row["side"], coid, new_status,
        )

        if new_status != "filled" or row["side"] != "sell":
            continue

        # Late-filled sell: the position is really gone. Record the deferred outcome
        # (best-effort — entry fill price comes from the opening buy order at the
        # broker) and clear ownership for both pools (long/flat: a sell is a full exit).
        exit_price = float(getattr(order, "filled_avg_price", 0) or 0)
        last_buy = repo.get_last_buy_order(row["symbol"])
        entry_price = 0.0
        if last_buy:
            buy_order = broker.get_order(last_buy["client_order_id"])
            entry_price = float(getattr(buy_order, "filled_avg_price", 0) or 0)
        if exit_price > 0 and entry_price > 0:
            try:
                repo.record_trade_outcome(TradeOutcomeRow(
                    symbol=row["symbol"],
                    strategy=row.get("strategy_name") or "unknown",
                    regime=row.get("regime") or "unknown",
                    side="buy", entry_price=entry_price, exit_price=exit_price,
                    pnl_pct=(exit_price - entry_price) / entry_price,
                    exit_reason="reconciled-exit",
                    entry_overlay_rationale=(last_buy or {}).get("entry_rationale"),
                    closed_at=datetime.now(timezone.utc).isoformat(),
                ))
            except Exception:
                logger.warning("reconciliation: outcome record failed for %s", row["symbol"])
        else:
            logger.warning(
                "reconciliation: missing fill prices for %s (entry=%.2f exit=%.2f) — "
                "outcome not recorded", row["symbol"], entry_price, exit_price,
            )
        for pool in ("daily", "intraday"):
            try:
                repo.clear_position_owner(row["symbol"], pool)
            except Exception:
                logger.warning(
                    "reconciliation: failed to clear owner for %s/%s", row["symbol"], pool
                )
    return updated


def precompute_signals(
    config: Config,
    strategies: list,
    asof: datetime,
    bars_cache: dict | None = None,
    cache_date: "object | None" = None,
) -> int:
    """Compute and cache buy/sell signals for all equity strategies after market close.

    Safe to call from the scheduler's post-close tick. Returns count of signals cached.
    Crypto and intraday strategies are skipped — they run on live bars, not daily bars.

    `cache_date` overrides the cache key date (defaults to `asof.date()`). Used on
    scheduler startup to repair a wiped in-memory cache mid-day: bars are fetched
    only through `asof` (kept at yesterday's close to avoid today's still-forming
    daily bar) but the result is filed under today's date so the market-open read
    path finds it immediately instead of recomputing off a live-moving bar.
    """
    import pandas as pd
    from datetime import date as _date
    today = cache_date if cache_date is not None else (asof.date() if hasattr(asof, "date") else asof)
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


def _stop_multiplier_for_owner(owner_name: str | None) -> float:
    """Resolve the stop-loss multiplier of the strategy class that owns a position.

    The owner is stored as a class name string; walk the Strategy subclass tree to
    find its stop_loss_multiplier (DipRecovery widens it to a catastrophe stop).
    Unknown/absent owner → 1.0 (the normal stop). Governs both software and broker stop.
    """
    if not owner_name:
        return 1.0
    from trader.strategy.base import Strategy

    seen = set()
    stack = list(Strategy.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        if cls.__name__ == owner_name:
            return float(getattr(cls, "stop_loss_multiplier", 1.0))
        stack.extend(cls.__subclasses__())
    return 1.0


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
    run_id = repo.record_run(strategy=type(strategy).__name__, mode=effective_autonomy(config))
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
        _owner = state.position_owners.get((symbol, _pool))
        _base_stop = (
            config.risk.crypto_stop_loss_pct if is_crypto_symbol(symbol)
            else config.risk.stop_loss_pct
        )
        _stop_pct = _base_stop * _stop_multiplier_for_owner(_owner)
        if (
            entry_price > 0
            and symbol in state.positions
            and state.positions[symbol] > 0
            and (current_price - entry_price) / entry_price <= -_stop_pct
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
            if not apply_fundamental_gate(symbol, bars, config, date_str, repo=repo):
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
            signal = apply_overlay(
                signal, bars, config,
                repo=repo, strategy_name=type(strategy).__name__, regime=classify_regime(bars),
            )

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
    options_broker: "AlpacaOptionsBroker | None" = None,
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

        # CSP-on-dip / Wheel entry: a DipRecovery-family buy signal on an
        # options-eligible symbol sells a cash-secured put instead of buying stock.
        # `type(strategy) is DipRecovery` (not isinstance) so plain DipRecovery only
        # routes here when CSP_ON_DIP_ENABLED; WheelStrategy (a DipRecovery subclass)
        # routes here whenever WHEEL_STRATEGY_ENABLED is on, tagged as "wheel".
        _wants_csp = (
            options_broker is not None
            and config.risk.options_trading_enabled
            and signal.side == "buy"
            and not is_crypto_symbol(symbol)
            and (
                (type(strategy) is DipRecovery and config.risk.csp_on_dip_enabled)
                or (isinstance(strategy, WheelStrategy) and config.risk.wheel_strategy_enabled)
            )
        )
        if _wants_csp:
            return _execute_csp_entry(
                signal=signal, run_id=run_id, strategy=strategy, config=config,
                options_broker=options_broker, repo=repo, gate=gate,
                kill_switch=kill_switch, state=state, asof=asof, ref_price=ref_price,
            )

        autonomy = effective_autonomy(config)
        # For auto-mode sells: cancel broker-side stop before gate evaluation so the
        # open stop order doesn't appear in open_order_symbols and block the sell.
        if signal.side == "sell" and autonomy == "auto" and not is_crypto_symbol(symbol):
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

        if autonomy == "manual":
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
        regime = classify_regime(bars)
        repo.record_order(OrderRow(
            client_order_id=client_order_id, symbol=symbol,
            side=signal.side, notional=risk_decision.approved_notional,
            status="submitted", broker_order_id=broker_order_id or None,
            strategy_name=type(strategy).__name__,
            regime=regime,
            signal_strength=signal.strength,
            entry_rationale=signal.reason if signal.side == "buy" else None,
        ))

        # Confirm the fill and persist the real status. record_order upserts on
        # client_order_id (ON CONFLICT DO UPDATE status), so this updates the
        # "submitted" row above rather than inserting a duplicate — without this,
        # every order stays "submitted" forever even after it fills on the broker.
        filled_order = broker.wait_for_fill(client_order_id)
        repo.record_order(OrderRow(
            client_order_id=client_order_id, symbol=symbol,
            side=signal.side, notional=risk_decision.approved_notional,
            status="filled" if filled_order else "submitted",
            broker_order_id=broker_order_id or None,
            strategy_name=type(strategy).__name__,
            regime=regime,
            signal_strength=signal.strength,
            entry_rationale=signal.reason if signal.side == "buy" else None,
        ))

        # Record the closed-trade outcome for the cooldown guard and overlay memory —
        # but only once the sell's fill is confirmed. An unconfirmed sell may never
        # fill (halted stock, rejected order); recording it anyway would write a
        # phantom closed trade and orphan the still-held position. Unconfirmed sells
        # stay 'submitted' and are settled later by reconcile_order_statuses.
        if signal.side == "sell" and filled_order is not None:
            entry_price = state.avg_entry_prices.get(symbol, 0.0)
            if entry_price > 0:
                if signal.reason.startswith("stop-loss:"):
                    exit_reason = "stop-loss"
                elif signal.reason.startswith("eod-exit:"):
                    exit_reason = "eod-exit"
                else:
                    exit_reason = "signal-exit"
                exit_price = float(
                    getattr(filled_order, "filled_avg_price", None) or ref_price
                )
                last_buy = repo.get_last_buy_order(symbol)
                entry_rationale = last_buy.get("entry_rationale") if last_buy else None
                try:
                    repo.record_trade_outcome(TradeOutcomeRow(
                        symbol=symbol, strategy=type(strategy).__name__, regime=regime,
                        side="buy", entry_price=entry_price, exit_price=exit_price,
                        pnl_pct=(exit_price - entry_price) / entry_price,
                        exit_reason=exit_reason, entry_overlay_rationale=entry_rationale,
                        closed_at=datetime.now(timezone.utc).isoformat(),
                    ))
                except Exception:
                    logger.warning("failed to record trade outcome for %s", symbol)
        elif signal.side == "sell":
            logger.warning(
                "%s sell not confirmed filled in time — outcome deferred to "
                "order-status reconciliation", symbol,
            )

        # Place a broker-side GTC stop to protect new long positions. Wait for the
        # buy to actually fill first — submitting a stop-sell before the fill lands
        # reads as a short sale and gets rejected by Alpaca for non-shortable assets.
        if signal.side == "buy" and not is_crypto_symbol(symbol):
            # Widen the broker stop by the buying strategy's multiplier so it matches
            # the software stop — a DipRecovery entry gets its catastrophe stop, not
            # the default 8% that would knife it out of a normal dip.
            _stop_pct = config.risk.stop_loss_pct * getattr(strategy, "stop_loss_multiplier", 1.0)
            stop_price = ref_price * (1 - _stop_pct)
            # filled_order already computed above (fill-status persistence step).
            if filled_order is None:
                logger.warning(
                    "%s buy not confirmed filled in time — skipping broker stop, "
                    "software stop remains active", symbol,
                )
            else:
                filled_qty = float(getattr(filled_order, "filled_qty", 0) or 0)
                stop_qty = filled_qty if filled_qty > 0 else round(
                    risk_decision.approved_notional / ref_price, 6
                )
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
            fill_confirmed=filled_order is not None,
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


def _execute_csp_entry(
    *,
    signal,
    run_id: int,
    strategy,
    config,
    options_broker: "AlpacaOptionsBroker",
    repo,
    gate,
    kill_switch,
    state,
    asof,
    ref_price: float,
):
    """Sell a cash-secured put on `signal.symbol` instead of buying stock.

    Manual-mode (proposal-queue) support is intentionally out of scope for v1 — the
    existing proposal/approval flow has no options-aware executor, so this path only
    runs under AUTONOMY=auto. Manual mode blocks with a clear reason rather than
    silently no-op-ing.
    """
    symbol = signal.symbol
    strategy_tag = "wheel" if isinstance(strategy, WheelStrategy) else "csp_on_dip"

    if effective_autonomy(config) != "auto":
        return PipelineRun(
            run_id=run_id, symbol=symbol, signal=signal,
            risk_decision=RiskDecision.reject(
                "options entries require AUTONOMY=auto (no manual-proposal support yet)"
            ),
            outcome="blocked",
        )

    if repo.get_open_options_positions(symbol):
        return PipelineRun(
            run_id=run_id, symbol=symbol, signal=signal,
            risk_decision=RiskDecision.reject(
                f"{symbol} already has an open wheel/CSP position"
            ),
            outcome="blocked",
        )

    budget = config.risk.max_options_allocation_pct * state.equity - state.options_collateral
    if budget <= 0:
        return PipelineRun(
            run_id=run_id, symbol=symbol, signal=signal,
            risk_decision=RiskDecision.reject("options allocation already at cap"),
            outcome="blocked",
        )

    contract = options_broker.select_csp_contract(symbol, ref_price=ref_price, max_collateral=budget)
    if contract is None:
        return PipelineRun(
            run_id=run_id, symbol=symbol, signal=signal,
            risk_decision=RiskDecision.reject(
                f"no liquid CSP contract for {symbol} fits budget ${budget:,.0f}"
            ),
            outcome="blocked",
        )

    spread = options_broker.check_spread(contract.symbol)
    if spread is not None and spread > config.risk.options_max_spread_pct:
        return PipelineRun(
            run_id=run_id, symbol=symbol, signal=signal,
            risk_decision=RiskDecision.reject(
                f"{contract.symbol} spread {spread:.1%} exceeds max {config.risk.options_max_spread_pct:.1%}"
            ),
            outcome="blocked",
        )

    collateral = contract.strike * 100.0
    risk_decision = gate.evaluate_options_order(
        OptionsOrderIntent(underlying=symbol, collateral=collateral), state, kill_switch,
    )
    logger.info(
        "options gate symbol=%s contract=%s approved=%s reason=%s",
        symbol, contract.symbol, risk_decision.approved, risk_decision.reason,
    )
    if not risk_decision.approved:
        return PipelineRun(
            run_id=run_id, symbol=symbol, signal=signal,
            risk_decision=risk_decision, outcome="blocked",
        )

    today = asof.date() if isinstance(asof, datetime) else asof
    # Include the resolved contract symbol in the ID so different contracts on the
    # same underlying on the same day produce distinct IDs and don't alias each other.
    client_order_id = options_client_order_id_for(today, contract.symbol, "put", strategy_tag)
    order = options_broker.sell_to_open(contract_symbol=contract.symbol, client_order_id=client_order_id)
    broker_order_id = str(getattr(order, "id", "") or "")

    repo.record_order(OrderRow(
        client_order_id=client_order_id, symbol=contract.symbol, side="sell",
        notional=collateral, status="submitted", broker_order_id=broker_order_id or None,
        strategy_name=type(strategy).__name__, regime=None,
        signal_strength=signal.strength, entry_rationale=signal.reason,
    ))

    repo.record_options_position(OptionsPositionRow(
        contract_symbol=contract.symbol, underlying=symbol, option_type="put",
        strike=contract.strike, expiry=contract.expiry.isoformat(),
        opening_order_id=client_order_id, strategy=strategy_tag,
        collateral=collateral, wheel_state="csp_open", status="open",
    ))

    send_alert(
        f"CSP-OPEN {contract.symbol} collateral=${collateral:,.0f} "
        f"{'paper' if config.alpaca_options_paper else 'LIVE'}",
        config.slack_webhook_url,
        alert_email=config.alert_email,
        smtp_user=config.smtp_user,
        smtp_password=config.smtp_password,
    )
    return PipelineRun(
        run_id=run_id, symbol=symbol, signal=signal,
        risk_decision=risk_decision, outcome="executed",
        order_id=client_order_id, is_options=True,
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
    run_id = repo.record_run(strategy=type(strategy).__name__, mode=effective_autonomy(config))

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

        if effective_autonomy(config) == "manual":
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
            broker_order_id = str(getattr(
                order_a if sym == sym_a else order_b, "id", ""
            ) or "") or None
            repo.record_order(OrderRow(
                client_order_id=oid, symbol=sym, side=side,
                notional=decision.approved_notional, status="submitted",
                broker_order_id=broker_order_id,
            ))
            # Confirm the fill and persist the real status — same upsert-on-
            # client_order_id pattern as the single-signal path.
            filled_order = broker.wait_for_fill(oid)
            repo.record_order(OrderRow(
                client_order_id=oid, symbol=sym, side=side,
                notional=decision.approved_notional,
                status="filled" if filled_order else "submitted",
                broker_order_id=broker_order_id,
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
