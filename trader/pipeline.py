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
from dataclasses import dataclass

_ALPACA_MIN_ORDER = 10.0
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from trader.alerts import send_alert
from trader.config import Config
from trader.data.alpaca_bars import get_daily_bars, get_daily_bars_batch, get_live_prices_batch, get_intraday_bars_batch
from trader.data.crypto_bars import get_crypto_bars
from trader.execution.broker import AlpacaBroker, client_order_id_for
from trader.execution.options_broker import AlpacaOptionsBroker, options_client_order_id_for
from trader.overlay import apply_earnings_gate, apply_fundamental_gate, apply_overlay
from trader.portfolio.repository import (
    OptionsPositionRow,
    OrderRow,
    ProposalRow,
    SignalRow,
    PortfolioRepository,
    TradeOutcomeRow,
)
from trader.risk.gate import (
    KillSwitch, OptionsOrderIntent, OrderIntent, RiskDecision, RiskGate,
    effective_autonomy, is_crypto_symbol, is_option_symbol,
)
from trader.risk.vol_sizing import vol_scale
from trader.strategy.dip_recovery import DipRecovery
from trader.strategy.regime import classify_regime
from trader.strategy.wheel import WheelStrategy

if TYPE_CHECKING:
    from trader.strategy.base import Signal, Strategy

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

# Tracks EOD exits already fired by (strategy_name, symbol, pool, date). Prevents
# repeated EOD exit signals for the same position on the same day if the first
# order is slow to fill or reconciliation lags.
_eod_exits_fired: dict[tuple[str, str, str, date], bool] = {}


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
    # Fail-safe default False: a run is "unconfirmed" until a code path proves the fill.
    # Only the sell path consults this (to decide whether to clear position ownership);
    # an unconfirmed sell must NOT clear ownership — reconcile_order_statuses settles it
    # later against broker truth. Buy/hold/blocked runs never read it.
    fill_confirmed: bool = False


def _purge_old_eod_exits(today: date) -> None:
    """Drop EOD-exit tracking keys for any date other than today.

    Key shape is (strategy_name, symbol, pool, date); the date is at index 3.
    Mutates in place so imported references stay valid in tests.
    """
    global _eod_exits_fired
    keys_to_drop = [
        key for key in _eod_exits_fired.keys()
        if key[3] != today
    ]
    for key in keys_to_drop:
        del _eod_exits_fired[key]


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
        # Fail-closed: without ownership data we cannot prevent duplicate buys or
        # cross-strategy sells. Treat the account state as stale for this tick.
        logger.exception("failed to load position owners from DB — marking state stale")
        from dataclasses import replace as _replace_stale
        state = _replace_stale(state, stale=True)

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
            # Silent staleness otherwise — stops evaluate against yesterday's close.
            # Alert once/day so the frequency is measurable; no skip/halt yet (measure first).
            if getattr(run_pipeline, "_quote_alert_date", None) != asof.date():
                send_alert(
                    "Live quote fetch failed — stop-loss evaluating against stale "
                    "(yesterday's) close this tick",
                    config.slack_webhook_url,
                    alert_email=config.alert_email,
                    smtp_user=config.smtp_user,
                    smtp_password=config.smtp_password,
                )
                run_pipeline._quote_alert_date = asof.date()  # type: ignore[attr-defined]
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

        # Compute each item's sort key once — classify_regime() is expensive and
        # sort would otherwise re-run it O(n log n) times per comparison.
        def _rank_key(item):
            raw, strat, _sig, bars, _, _pool = item
            arm = (type(strat).__name__, classify_regime(bars))
            w, _ = _bandit_w.get(arm, (1.0, 0))
            effective = raw * w
            if config.risk.bandit_weighting_live:
                return effective
            logger.info(
                "bandit shadow arm=%s effective=%.4f raw=%.4f weight=%.4f",
                arm, effective, raw, w,
            )
            return raw

        pending_buys = [item for _, item in
                        sorted(((_rank_key(it), it) for it in pending_buys),
                               key=lambda pair: pair[0], reverse=True)]
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
                # Confirmed sells (including EOD/stop exits injected by the pipeline)
                # must reset the strategy's entry-tracking state immediately so a
                # stale _entry_bar_ts / _entry_price cannot survive until the next tick.
                strategy.reset_state()
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
        try:
            order = broker.get_order(coid)
        except Exception:
            # A transient broker error on one row must not abort the whole batch —
            # the orphaned sells this job settles may be later in the list.
            logger.warning("reconciliation: broker lookup failed for %s — retry next pass", coid)
            continue
        if order is None:
            continue  # lookup failed — status unknown, retry next pass
        status = str(getattr(order, "status", "")).lower()
        if status == "filled":
            new_status = "filled"
        elif status in {"canceled", "cancelled", "expired", "rejected"}:
            new_status = "canceled" if status == "cancelled" else status
        else:
            continue  # still live (new/accepted/partially_filled) — leave as submitted

        try:
            repo.record_order(OrderRow(
                client_order_id=coid, symbol=row["symbol"], side=row["side"],
                notional=row["notional"], status=new_status,
            ))
        except Exception:
            # One bad row must not abandon the rest of the batch — the orphaned sells
            # this job exists to settle may be later in the list.
            logger.warning("reconciliation: status upsert failed for %s — skipping row", coid)
            continue
        updated += 1
        logger.info(
            "order reconciliation: %s %s %s -> %s",
            row["symbol"], row["side"], coid, new_status,
        )

        # Options orders (CSP sell-to-open) also land here as side="sell"/"submitted",
        # but a sell-to-open is an ENTRY, not an equity exit — the equity outcome/owner
        # logic below would mishandle it. Their status is already upserted above; the
        # wheel-state/assignment reconciliation is reconcile_options' job (P3.2).
        if new_status != "filled" or row["side"] != "sell" or is_option_symbol(row["symbol"]):
            continue

        # Late-filled sell: the position is really gone. Record the deferred outcome
        # (best-effort — entry fill price comes from the opening buy order at the
        # broker) and clear ownership for both pools (long/flat: a sell is a full exit).
        exit_price = float(getattr(order, "filled_avg_price", 0) or 0)
        try:
            last_buy = repo.get_last_buy_order(row["symbol"])
            entry_price = 0.0
            if last_buy:
                buy_order = broker.get_order(last_buy["client_order_id"])
                entry_price = float(getattr(buy_order, "filled_avg_price", 0) or 0)
        except Exception:
            # Entry-price lookup is best-effort — a failure here must not abort the
            # batch or skip the ownership cleanup below; just defer the outcome record.
            logger.warning(
                "reconciliation: entry-price lookup failed for %s — outcome deferred",
                row["symbol"],
            )
            last_buy, entry_price = None, 0.0
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
        # Clear ownership only for the pool this order's strategy actually owns; a symbol
        # can be held independently in both the daily and intraday pools, and clearing
        # both would wrongly release the other strategy's position.
        strategy_name = row.get("strategy_name")
        for (sym, pool), owner in repo.get_position_owners().items():
            if sym != row["symbol"]:
                continue
            if strategy_name is not None and owner != strategy_name:
                continue  # different strategy owns this pool — leave it
            try:
                repo.clear_position_owner(sym, pool)
            except Exception:
                logger.warning(
                    "reconciliation: failed to clear owner for %s/%s", sym, pool
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


def _log_decision_features(*, config, repo, run_id, signal, bars, strategy_name, regime, mode) -> None:
    """Best-effort feature-snapshot log. Never raises — must not affect the overlay.

    Re-derives news/sentiment/fundamentals via the same Finnhub client singletons
    the overlay uses. News and fundamentals fetches go through
    trader.overlay.news_context._fetch_finnhub_articles_classified and
    trader.overlay.fundamental_gate.fetch_fundamentals_raw respectively — both
    functions carry their own 60-second TTL cache (keyed by (symbol, api_key)
    and symbol respectively) that is ALSO consulted by apply_claude_overlay's
    Finnhub calls (via fetch_news_finnhub / check_fundamental_gate). Because the
    cache lives inside the shared fetch functions rather than here, a cold tick
    issues at most one Finnhub call per symbol per minute regardless of how many
    times the pipeline touches that symbol — no coordination needed between this
    function and the overlay.
    """
    try:
        from trader.ml_overlay.features import build_feature_vector
        from trader.overlay import _get_finnhub_client, _get_sentiment_client
        from trader.overlay.news_context import _fetch_finnhub_articles_classified
        from trader.overlay.fundamental_gate import fetch_fundamentals_raw, parse_fundamentals_finnhub
        from trader.portfolio.repository import DecisionFeaturesRow

        finnhub_client = _get_finnhub_client(config)
        news_categories: dict = {}
        finnhub_key = getattr(config, "finnhub_api_key", None)
        if finnhub_key:
            try:
                news_categories = _fetch_finnhub_articles_classified(signal.symbol, finnhub_key)
            except Exception:
                news_categories = {}

        sentiment = None
        if "/" in signal.symbol:
            sentiment_client = _get_sentiment_client(config, finnhub_client)
            if sentiment_client is not None:
                # SentimentClient.get_sentiment has its own 4-hour in-process
                # cache in trader/data/sentiment_client.py; no cache layer needed here.
                sentiment = sentiment_client.get_sentiment(signal.symbol)

        fundamentals: dict = {}
        if finnhub_client is not None and "/" not in signal.symbol:
            try:
                metrics, recs = fetch_fundamentals_raw(signal.symbol, finnhub_client)
                fundamentals = parse_fundamentals_finnhub(metrics, recs)
            except Exception:
                fundamentals = {}

        recent_outcomes = []
        if repo is not None:
            try:
                recent_outcomes = repo.get_recent_outcomes(symbol=signal.symbol, limit=3)
            except Exception:
                recent_outcomes = []

        features = build_feature_vector(
            signal, bars, news_categories=news_categories, sentiment=sentiment,
            fundamentals=fundamentals, recent_outcomes=recent_outcomes, regime=regime,
        )

        if repo is not None:
            # Postgres's decision_features table (migration 008) has
            # CHECK (mode IN ('auto', 'manual')). `mode` comes from
            # effective_autonomy(config), which reads an unvalidated config
            # string — clamp here so a drifted autonomy value can't silently
            # fail every insert (swallowed by the except below) instead of
            # just this row. "manual" is the conservative fallback.
            safe_mode = mode if mode in ("auto", "manual") else "manual"
            repo.record_decision_features(DecisionFeaturesRow(
                run_id=run_id, symbol=signal.symbol, side=signal.side,
                strategy=strategy_name, regime=regime, mode=safe_mode,
                signal_strength_pre_overlay=signal.strength,
                features=features,
            ))
    except Exception:
        logger.warning("decision-features logging failed for %s", signal.symbol, exc_info=True)


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
        _owner = state.position_owners.get((symbol, _pool))
        if not strategy._warmed_up:
            _has_position = symbol in state.positions and state.positions[symbol] > 0
            if _has_position:
                if _owner is None or _owner == type(strategy).__name__:
                    strategy.warm_up(bars, has_position=True)
                else:
                    strategy._warmed_up = True
            else:
                # No open position: just mark warmed up so the strategy can start
                # generating fresh entry signals on the next tick.
                strategy._warmed_up = True

        # If this strategy is not the recorded owner of the position in this pool,
        # reset any stale entry-tracking state so it can generate fresh entries
        # signals instead of trying to manage a position it does not own.
        if _owner is not None and _owner != type(strategy).__name__:
            strategy.reset_state()

        current_price = (live_prices or {}).get(symbol) or float(bars["close"].iloc[-1])
        # Anchor to the highest price paid across open lots, not the broker's averaged
        # cost basis — averaging down (a second buy at a lower price) must not mute the
        # stop distance on the earlier, more-underwater lot. Falls back to avg cost if
        # no local fill-price history exists yet (e.g. pre-migration positions).
        if symbol in state.positions and state.positions[symbol] > 0:
            try:
                entry_price = repo.get_highest_buy_price(symbol) or state.avg_entry_prices.get(symbol, 0.0)
            except Exception:
                entry_price = state.avg_entry_prices.get(symbol, 0.0)
        else:
            entry_price = state.avg_entry_prices.get(symbol, 0.0)
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
                        _today = asof.date() if hasattr(asof, "date") else asof
                        _eod_key = (type(strategy).__name__, symbol, _pool, _today)
                        if _eod_key not in _eod_exits_fired:
                            signal = Signal(
                                symbol, "sell", 1.0,
                                f"eod-exit: intraday flat at {_asof_ny.strftime('%H:%M')} ET",
                            )
                            _eod_exits_fired[_eod_key] = True
                            # Prevent unbounded growth if the process runs across many days.
                            _purge_old_eod_exits(_today)
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

        # Buy-side ownership conflict — a different strategy already holds this symbol
        # in this pool. Without this, two arms can independently buy into the same
        # symbol (e.g. NNBR: SuperTrend and DipRecovery both bought within 3 minutes
        # on 2026-07-16, both got stopped out separately).
        if signal.side == "buy":
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

        if signal.side == "buy" and not is_crypto_symbol(symbol) and not _is_intraday:
            date_str = asof.strftime("%Y-%m-%d")
            if not apply_earnings_gate(symbol, config, date_str):
                veto_signal = Signal(
                    symbol, "hold", 0.0,
                    "[earnings gate veto] earnings release within window",
                )
                repo.record_signal(SignalRow(
                    run_id=run_id, symbol=symbol,
                    side=veto_signal.side, strength=veto_signal.strength, reason=veto_signal.reason,
                ))
                return PipelineRun(
                    run_id=run_id, symbol=symbol, signal=veto_signal,
                    risk_decision=RiskDecision.reject("earnings gate veto"), outcome="hold",
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
            regime = classify_regime(bars)
            _log_decision_features(
                config=config, repo=repo, run_id=run_id, signal=signal,
                bars=bars, strategy_name=type(strategy).__name__,
                regime=regime, mode=effective_autonomy(config),
            )
            signal = apply_overlay(
                signal, bars, config,
                repo=repo, strategy_name=type(strategy).__name__, regime=regime,
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
        # For auto-mode sells: exclude the symbol's resting stop from open_order_symbols so
        # it doesn't read as an in-flight order that blocks the sell during gate eval. This
        # is a local view only — the live stop is NOT canceled until the sell is approved,
        # so a gate rejection leaves the position protected.
        _sell_needs_stop_cancel = (
            signal.side == "sell" and autonomy == "auto" and not is_crypto_symbol(symbol)
        )
        if _sell_needs_stop_cancel:
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
        # Now that the sell is approved, cancel the resting protective stop so it can't
        # double-sell alongside this order. Deferred to here (post-approval) so a rejected
        # sell never strips the stop off a position we're still holding.
        if _sell_needs_stop_cancel:
            broker.cancel_open_stops(symbol)
        # Software stop-loss sells get a limit floor so a gap-down on a thin name can't
        # slip past it unbounded (see broker.submit docstring) — same cap already used
        # for the resting broker-side GTC stop.
        _sell_limit_floor_pct = (
            config.risk.stop_limit_slippage_pct
            if signal.side == "sell" and signal.reason.startswith("stop-loss")
            else None
        )
        order = broker.submit(
            symbol=symbol, side=signal.side,
            client_order_id=client_order_id,
            notional=risk_decision.approved_notional if signal.side == "buy" else None,
            qty=qty if signal.side == "sell" else None,
            ref_price=ref_price,
            sell_limit_floor_pct=_sell_limit_floor_pct,
        )
        broker_order_id = str(getattr(order, "id", "") or "")
        regime = classify_regime(bars)
        order_id = repo.record_order(OrderRow(
            client_order_id=client_order_id, symbol=symbol,
            side=signal.side, notional=risk_decision.approved_notional,
            status="submitted", broker_order_id=broker_order_id or None,
            strategy_name=type(strategy).__name__,
            regime=regime,
            signal_strength=signal.strength,
            entry_rationale=signal.reason if signal.side == "buy" else None,
        ))
        try:
            repo.link_order_to_decision_features(run_id=run_id, order_id=order_id)
        except Exception:
            logger.warning("decision-features order-link failed for %s", symbol, exc_info=True)

        # Confirm the fill and persist the real status. record_order upserts on
        # client_order_id (ON CONFLICT DO UPDATE status), so this updates the
        # "submitted" row above rather than inserting a duplicate — without this,
        # every order stays "submitted" forever even after it fills on the broker.
        filled_order = broker.wait_for_fill(client_order_id)
        _fill_price = (
            float(getattr(filled_order, "filled_avg_price", None) or 0) or None
            if filled_order else None
        )
        repo.record_order(OrderRow(
            client_order_id=client_order_id, symbol=symbol,
            side=signal.side, notional=risk_decision.approved_notional,
            status="filled" if filled_order else "submitted",
            broker_order_id=broker_order_id or None,
            strategy_name=type(strategy).__name__,
            regime=regime,
            signal_strength=signal.strength,
            entry_rationale=signal.reason if signal.side == "buy" else None,
            fill_price=_fill_price,
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
            # filled_order already computed above (fill-status persistence step).
            if filled_order is None:
                logger.warning(
                    "%s buy not confirmed filled in time — skipping broker stop, "
                    "software stop remains active", symbol,
                )
            else:
                # Anchor the stop to the actual fill, not the pre-trade ref price, so the
                # configured stop distance holds even when the fill slipped from ref. When
                # this is an add to an existing position, anchor to the highest price paid
                # across all open lots (not this fill alone) — averaging down must not let
                # an already-underwater lot ride past its own stop distance.
                fill_px = float(getattr(filled_order, "filled_avg_price", None) or ref_price)
                try:
                    _highest_buy = repo.get_highest_buy_price(symbol)
                except Exception:
                    logger.warning("get_highest_buy_price failed for %s — using this fill only", symbol)
                    _highest_buy = None
                stop_anchor = max(fill_px, _highest_buy or 0.0)
                stop_price = stop_anchor * (1 - _stop_pct)
                filled_qty = float(getattr(filled_order, "filled_qty", 0) or 0)
                new_qty = filled_qty if filled_qty > 0 else round(
                    risk_decision.approved_notional / ref_price, 6
                )
                # cancel_open_stops below removes the stop covering any already-held
                # shares too, so the replacement must cover the full position, not just
                # this fill — otherwise the earlier lot rides unprotected.
                held_before = state.positions.get(symbol, 0.0)
                stop_qty = held_before + new_qty
                # Keyed on this buy fill's own broker_order_id, not just
                # date|symbol|side|strategy — two buys on the same symbol by the same
                # strategy on the same day used to hash to the same stop_oid, so the
                # second placement collided with the first (Alpaca rejects the reused
                # id as a duplicate and hands back the stale, already-cancelled order —
                # leaving the position with no live stop). broker_order_id is unique per
                # real fill, so distinct buys now get distinct stop ids, while a retry of
                # *this* fill's stop placement still reuses the same id (idempotent).
                stop_oid = client_order_id_for(
                    today, symbol, "sell",
                    f"stop-{type(strategy).__name__}-{broker_order_id}",
                )
                try:
                    broker.cancel_open_stops(symbol)
                    broker.place_stop_order(
                        symbol=symbol, qty=stop_qty,
                        stop_price=stop_price, client_order_id=stop_oid,
                        limit_offset_pct=config.risk.stop_limit_slippage_pct,
                    )
                    logger.info(
                        "placed GTC stop for %s at %.2f (anchor %.4f, qty %.4f)",
                        symbol, stop_price, stop_anchor, stop_qty,
                    )
                except Exception:
                    logger.exception("stop order failed for %s — software stop remains active", symbol)
                    send_alert(
                        f"BROKER STOP FAILED {symbol}: position unprotected — "
                        f"software stop still active",
                        config.slack_webhook_url,
                        alert_email=config.alert_email,
                        smtp_user=config.smtp_user,
                        smtp_password=config.smtp_password,
                    )
                    # If the deployment requires a broker-side stop, a failed stop
                    # placement is not acceptable. Cancel the newly-bought shares
                    # so the position does not ride without protection.
                    if config.risk.require_broker_stop:
                        logger.warning(
                            "require_broker_stop enabled — cancelling %s buy to avoid unprotected position",
                            symbol,
                        )
                        _cancel_client_order_id = client_order_id_for(
                            today, symbol, "buy", type(strategy).__name__
                        )
                        try:
                            broker.cancel_open_stops(symbol)
                            _open_order = broker.get_order(_cancel_client_order_id)
                            if _open_order is not None:
                                _oid = str(getattr(_open_order, "id", "") or "")
                                if _oid:
                                    broker.cancel_order_by_id(_oid)
                                # Only record "canceled" if the broker confirms the
                                # order is no longer open. cancel_order_by_id swallows
                                # exceptions, so we must verify.
                                _check = broker.get_order(_cancel_client_order_id)
                                _status = (
                                    str(getattr(_check, "status", "") or "").lower()
                                    if _check is not None
                                    else ""
                                )
                                _canceled = _status in {"canceled", "cancelled"}
                                if _check is None or _canceled:
                                    repo.record_order(OrderRow(
                                        client_order_id=_cancel_client_order_id,
                                        symbol=symbol, side="buy",
                                        notional=risk_decision.approved_notional,
                                        status="canceled",
                                    ))
                                else:
                                    logger.warning(
                                        "could not verify cancellation of %s buy (status=%s); "
                                        "leaving order record as-is",
                                        symbol, _status or "unknown",
                                    )
                            else:
                                # Order already gone from broker; nothing to record.
                                logger.warning(
                                    "%s buy order %s not found for cancellation after stop failure",
                                    symbol, _cancel_client_order_id,
                                )
                        except Exception:
                            logger.exception("failed to cancel %s buy after stop failure", symbol)
                        return PipelineRun(
                            run_id=run_id, symbol=symbol, signal=signal,
                            risk_decision=RiskDecision.reject(
                                "broker stop placement failed — buy cancelled"
                            ),
                            outcome="blocked", order_id=client_order_id,
                        )

        _env = "paper" if config.alpaca_paper else "LIVE"
        # "FILL" only when the fill is confirmed. An unconfirmed sell still holds the
        # position (outcome deferred to reconciliation) — telling the operator it filled
        # would defeat the whole fill_confirmed guard at the notification layer.
        _tag = "FILL" if filled_order is not None else "SUBMITTED(unconfirmed)"
        send_alert(
            f"{_tag} {symbol} {signal.side.upper()} ${risk_decision.approved_notional:.0f} {_env}",
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

    order_id = repo.record_order(OrderRow(
        client_order_id=client_order_id, symbol=contract.symbol, side="sell",
        notional=collateral, status="submitted", broker_order_id=broker_order_id or None,
        strategy_name=type(strategy).__name__, regime=None,
        signal_strength=signal.strength, entry_rationale=signal.reason,
    ))
    try:
        repo.link_order_to_decision_features(run_id=run_id, order_id=order_id)
    except Exception:
        logger.warning("decision-features order-link failed for %s", contract.symbol, exc_info=True)

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
