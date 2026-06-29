"""Market-hours scheduler using Alpaca's clock API.

Run as a blocking process from the command line:
    python -m trader.scheduler

The Streamlit dashboard runs separately (shares the same SQLite DB via WAL mode).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from trader.alerts import send_alert
from trader.config import Config, load_config
from trader.execution.broker import AlpacaBroker
from trader.pipeline import PipelineRun, run_pipeline
from trader.portfolio.repository import PortfolioRepository
from trader.portfolio.postgres_repo import PostgresRepository
from trader.risk.gate import KillSwitch, is_crypto_symbol

if TYPE_CHECKING:
    from trader.strategy.base import Strategy

logger = logging.getLogger(__name__)


def is_market_open(broker: AlpacaBroker, *, symbol: str = "") -> bool:
    """Return True if the market is open for the given symbol.

    Crypto (symbol contains '/') is always open — 24/7.
    Equities: check Alpaca's clock. Fails closed on any API error.
    Existing callers that pass only `broker` continue to work (symbol defaults to '').
    """
    if is_crypto_symbol(symbol):
        return True
    try:
        client = broker._ensure_client()
        clock = client.get_clock()
        return bool(clock.is_open)
    except Exception:
        logger.exception("clock check failed; treating market as closed")
        return False


def run_once(
    config: Config,
    strategies: "list[Strategy]",
    broker: AlpacaBroker,
    repo: PortfolioRepository,
) -> list[PipelineRun]:
    """Single pipeline tick. Called by the scheduler loop and directly in smoke tests.

    Returns an empty list (no-op) when the market is closed or the kill switch is engaged.
    """
    if not is_market_open(broker):
        logger.debug("market closed — skipping pipeline tick")
        return []
    ks = KillSwitch(config.kill_switch_path)
    if ks.engaged():
        logger.warning("kill switch engaged — skipping pipeline tick")
        _today = datetime.now(timezone.utc).date()
        if getattr(run_once, "_kill_switch_alert_date", None) != _today:
            send_alert(
                "Kill switch engaged — trading halted",
                config.slack_webhook_url,
                alert_email=config.alert_email,
                smtp_user=config.smtp_user,
                smtp_password=config.smtp_password,
            )
            run_once._kill_switch_alert_date = _today  # type: ignore[attr-defined]
        return []
    return run_pipeline(config, strategies, broker, repo)


def run_nightly_bandit_update(
    config: Config,
    broker: AlpacaBroker,
    repo: PortfolioRepository,
    cycle_index: int = 0,
) -> dict[tuple[str, str], float]:
    """Nightly EWMA bandit-weight refresh from the day's realized fills.

    No-op unless bandit weighting is enabled (shadow or live) — avoids hitting the
    broker activities API when the feature is off. Fetches FILL activities, joins
    them to recorded orders for (strategy, regime) context, and updates per-arm
    weights. Both shadow and live update weights; only the pipeline differs on
    whether it acts on them.
    """
    if not (config.risk.bandit_weighting_shadow or config.risk.bandit_weighting_live):
        return {}

    from trader.learning.update_weights import record_ic_observations, update_bandit_weights  # noqa: F401 — record_ic_observations wired here when bars_cache available
    # TODO: wire record_ic_observations after compute_ic_from_broker_fills

    try:
        fills = broker.get_account_activities(activity_type="FILL")
    except Exception:
        logger.exception("nightly bandit update: failed to fetch account activities")
        return {}

    weights = update_bandit_weights(repo, fills=fills, cycle_index=cycle_index)
    logger.info("nightly bandit update: refreshed %d arm(s)", len(weights))
    return weights


def start_scheduler(
    config: Config,
    strategies: "list[Strategy]",
    broker: AlpacaBroker,
    repo: PortfolioRepository,
    poll_minutes: int = 1,
) -> None:
    """Blocking scheduler loop. Checks the Alpaca clock before each tick.

    In dynamic-universe mode (DYNAMIC_UNIVERSE=true), rebuilds the strategy list
    once per calendar day at the first market-open tick. Always includes symbols
    with open positions so stop-loss logic never goes dark on a held stock.

    Runs until interrupted (KeyboardInterrupt) or killed.
    """
    logger.info(
        "scheduler starting — autonomy=%s poll=%dm dynamic_universe=%s symbols=%s",
        config.autonomy,
        poll_minutes,
        config.risk.dynamic_universe,
        [s.symbol for s in strategies],
    )
    current_strategies = strategies
    universe_date: date | None = None
    bandit_update_date: date | None = None
    signal_precomputed_date: date | None = None
    bandit_enabled = config.risk.bandit_weighting_shadow or config.risk.bandit_weighting_live

    while True:
        try:
            market_open = is_market_open(broker)

            # Weekly universe refresh — runs once per week on Monday's first open tick.
            # First run (universe_date is None) always refreshes regardless of day.
            if config.risk.dynamic_universe and market_open:
                today = date.today()
                first_run = universe_date is None
                if universe_date != today and (first_run or today.weekday() == 0):
                    current_strategies = _refresh_dynamic_universe(
                        config, broker, current_strategies
                    )
                    universe_date = today

            # Post-close signal precompute — runs once per day on the first closed tick.
            # Caches strategy.generate() output so market-open tick skips recomputation.
            if not market_open:
                today = date.today()
                if signal_precomputed_date != today:
                    from trader.pipeline import precompute_signals
                    precompute_signals(config, current_strategies, datetime.now(timezone.utc))
                    signal_precomputed_date = today

            # Nightly bandit refresh — runs once per calendar day on the first closed
            # tick, so the day's fills are settled before the EWMA update.
            if bandit_enabled and not market_open:
                today = date.today()
                if bandit_update_date != today:
                    cycle_index = 1 + max(
                        (ci for _, ci in repo.get_all_bandit_weights().values()),
                        default=0,
                    )
                    run_nightly_bandit_update(config, broker, repo, cycle_index=cycle_index)
                    bandit_update_date = today

            results = run_once(config, current_strategies, broker, repo)
            for r in results:
                logger.info("tick result: symbol=%s outcome=%s", r.symbol, r.outcome)
        except Exception:
            logger.exception("unhandled error in scheduler tick — continuing")
        time.sleep(poll_minutes * 60)


def run_once_crypto(
    config: Config,
    strategies: "list[Strategy]",
    broker: AlpacaBroker,
    repo: PortfolioRepository,
) -> list[PipelineRun]:
    """Single crypto pipeline tick. Skips the market-hours check (crypto is 24/7).
    Only the kill switch can halt execution here.
    """
    ks = KillSwitch(config.kill_switch_path)
    if ks.engaged():
        logger.warning("kill switch engaged — skipping crypto pipeline tick")
        _today = datetime.now(timezone.utc).date()
        if getattr(run_once_crypto, "_kill_switch_alert_date", None) != _today:
            send_alert(
                "Kill switch engaged — crypto trading halted",
                config.slack_webhook_url,
                alert_email=config.alert_email,
                smtp_user=config.smtp_user,
                smtp_password=config.smtp_password,
            )
            run_once_crypto._kill_switch_alert_date = _today  # type: ignore[attr-defined]
        return []
    return run_pipeline(config, strategies, broker, repo)


def start_crypto_scheduler(
    config: Config,
    strategies: "list[Strategy]",
    broker: AlpacaBroker,
    repo: PortfolioRepository,
    poll_minutes: int = 5,
) -> None:
    """Blocking scheduler loop for crypto strategies. Runs 24/7 (no market-hours check).

    In dynamic mode (DYNAMIC_CRYPTO_UNIVERSE=true), rebuilds strategy list once per
    calendar day by ranking CRYPTO_CANDIDATE_UNIVERSE by 24h volume. Always retains
    symbols with open positions for stop-loss coverage.

    IMPORTANT: crypto symbols MUST be in CRYPTO_ALLOWLIST, not RISK_ALLOWLIST.
    """
    logger.info(
        "crypto scheduler starting — autonomy=%s poll=%dm dynamic_crypto=%s symbols=%s",
        config.autonomy,
        poll_minutes,
        config.risk.dynamic_crypto_universe,
        [s.symbol for s in strategies],
    )
    current_strategies = strategies
    crypto_universe_date: date | None = None

    while True:
        try:
            if config.risk.dynamic_crypto_universe:
                today = date.today()
                first_run = crypto_universe_date is None
                if crypto_universe_date != today and (first_run or today.weekday() == 0):
                    current_strategies = _refresh_dynamic_crypto_universe(
                        config, broker, current_strategies
                    )
                    crypto_universe_date = today

            results = run_once_crypto(config, current_strategies, broker, repo)
            for r in results:
                logger.info("crypto tick: symbol=%s outcome=%s", r.symbol, r.outcome)
        except Exception:
            logger.exception("unhandled error in crypto scheduler tick — continuing")
        time.sleep(poll_minutes * 60)


def _build_strategies_for(config: Config, symbols: "list[str]") -> "list[Strategy]":
    """Build 2-strategy stack (SuperTrend + DipRecovery) per equity symbol.

    Combination backtests (scripts/backtest_combos.py; 10 symbols, 2yr and 4yr
    split-adjusted windows, independent-sleeve model matching live execution):

      ST+Dip:        2yr 37.9% / Sharpe 0.91   4yr 151.3% / Sharpe 1.00
      All 5 (prior): 2yr 23.8% / Sharpe 0.86   4yr  89.6% / Sharpe 0.98

    SuperTrend (trend-following) and DipRecovery (deep-drawdown mean reversion)
    are complementary; SmashDayB, EquityBollingerReversion and DonchianBreakout
    each diluted returns at similar or worse drawdown on equities.
    """
    # Previous 5-strategy stack — kept for easy rollback if ST+Dip underperforms
    # live. To restore, replace the loop body below with:
    #
    # from trader.strategy.smash_day import SmashDayB
    # from trader.strategy.equity_reversion import EquityBollingerReversion
    # from trader.strategy.donchian_breakout import DonchianBreakout
    # ...
    #     strategies.append(SuperTrend(symbol=sym))
    #     strategies.append(SmashDayB(symbol=sym, long_only=True))
    #     strategies.append(EquityBollingerReversion(symbol=sym))
    #     strategies.append(DonchianBreakout(symbol=sym))
    #     strategies.append(DipRecovery(symbol=sym))

    from trader.strategy.supertrend import SuperTrend
    from trader.strategy.dip_recovery import DipRecovery

    strategies: list[Strategy] = []
    for sym in symbols:
        strategies.append(SuperTrend(symbol=sym))
        strategies.append(DipRecovery(symbol=sym))
    return strategies


def _build_intraday_strategies_for(symbols: "list[str]") -> "list[Strategy]":
    """Build all 4 intraday strategies per symbol.

    Uses INTRADAY_ALLOWLIST env var; falls back to the same symbols as the equity stack.
    """
    from trader.strategy.intraday_trend import IntradayTrend
    from trader.strategy.vwap_reversion import VWAPReversion
    from trader.strategy.gap_and_go import GapAndGo
    from trader.strategy.orb import OpeningRangeBreakout

    strategies: list[Strategy] = []
    for sym in symbols:
        strategies.append(IntradayTrend(symbol=sym))
        strategies.append(VWAPReversion(symbol=sym))
        strategies.append(GapAndGo(symbol=sym))
        strategies.append(OpeningRangeBreakout(symbol=sym))
    return strategies


def _refresh_dynamic_universe(
    config: Config,
    broker: AlpacaBroker,
    current_strategies: "list[Strategy]",
) -> "list[Strategy]":
    """Screen for today's universe and rebuild strategies with position-safety guarantee.

    Always includes symbols from open positions so stop-loss logic never goes dark
    on a held stock that falls out of today's screened universe.
    Falls back to the existing strategy list if the screener fails.
    """
    from trader.universe.screener import fetch_dynamic_universe

    # Snapshot current positions before rebuilding — guarantees stop-loss coverage.
    try:
        state = broker.reconcile()
        held_symbols = set(state.positions.keys()) if not state.stale else set()
    except Exception:
        logger.exception("reconcile failed during universe refresh — using held_symbols=empty")
        held_symbols = set()

    try:
        screened = fetch_dynamic_universe(config, config.risk.universe_size)
    except Exception:
        logger.exception(
            "screener failed — keeping existing %d strategies", len(current_strategies)
        )
        return current_strategies

    # Union: today's screen + all held positions (held always wins for stop-loss safety).
    all_symbols: list[str] = list(dict.fromkeys(screened + list(held_symbols)))

    # Reuse existing instances for symbols already in the universe — preserves
    # stateful exit tracking (e.g. DonchianBreakout entry timestamps). Only create
    # fresh instances for symbols entering the universe for the first time today.
    existing: dict[str, list] = {}
    for s in current_strategies:
        existing.setdefault(s.symbol, []).append(s)
    new_strategies: list = []
    new_count = 0
    for sym in all_symbols:
        if sym in existing:
            new_strategies.extend(existing[sym])
        else:
            new_strategies.extend(_build_strategies_for(config, [sym]))
            new_count += 1

    logger.info(
        "universe refreshed: %d screened + %d held = %d symbols → %d strategies (%d new)",
        len(screened),
        len(held_symbols),
        len(all_symbols),
        len(new_strategies),
        new_count,
    )
    return new_strategies


def _build_crypto_strategies_for(config: Config, symbols: "list[str]") -> "list[Strategy]":
    """Build single-strategy stack (DonchianBreakout) per crypto symbol.

    Pure Donchian beat every tested combo on crypto across both the 2yr and 4yr
    windows (7 pairs, 10bps slippage):

      2yr: 56% ret / Sharpe 0.46 / -33% dd   vs EMA+Smash+Dip 21% / 0.22 / -51%
      4yr: 205% ret / Sharpe 0.51 / -42% dd  vs EMA+Smash+Dip 132% / 0.41 / -56%

    Every strategy added to it diluted returns AND worsened drawdown — the
    composite's sell-priority lets weaker strategies' sells truncate Donchian's
    big winners (XRP +265%, SOL +725%, DOGE +365% standalone). SuperTrend,
    HAPullback and EquityBollingerReversion also tested negative on crypto.
    DipRecovery only helped the old EMA+Smash stack, which is now retired.
    """
    # Previous stack (EMA crossover + SmashDayB + DipRecovery 30/10) — kept for easy
    # rollback if pure Donchian underperforms live. To restore, replace the return
    # below with:
    #
    # from trader.strategy.crypto_trend import CryptoEMACrossover
    # from trader.strategy.smash_day import SmashDayB
    # from trader.strategy.dip_recovery import DipRecovery
    # strategies: list[Strategy] = []
    # for sym in symbols:
    #     strategies.append(CryptoEMACrossover(symbol=sym))
    #     strategies.append(SmashDayB(symbol=sym, long_only=True))
    #     strategies.append(DipRecovery(symbol=sym, dip_pct=0.30, expansion_pct=0.10))
    # return strategies

    from trader.strategy.donchian_breakout import DonchianBreakout
    return [DonchianBreakout(symbol=sym) for sym in symbols]


def _build_crypto_strategies(config: Config) -> "list[Strategy]":
    """Build crypto strategy stack from config's crypto_allowlist (static mode)."""
    return _build_crypto_strategies_for(config, list(config.risk.crypto_allowlist or []))


def _refresh_dynamic_crypto_universe(
    config: Config,
    broker: AlpacaBroker,
    current_strategies: "list[Strategy]",
) -> "list[Strategy]":
    """Rank candidate crypto pairs by 24h volume and rebuild strategies.

    Always includes symbols from open crypto positions so stop-loss never goes dark.
    Falls back to existing strategy list if screener fails.
    """
    from trader.universe.crypto_screener import fetch_dynamic_crypto_universe

    try:
        state = broker.reconcile()
        held_symbols = {s for s in state.positions if is_crypto_symbol(s)} if not state.stale else set()
    except Exception:
        logger.exception("reconcile failed during crypto universe refresh — using held_symbols=empty")
        held_symbols = set()

    try:
        screened = fetch_dynamic_crypto_universe(config, config.risk.crypto_universe_size)
    except Exception:
        logger.exception(
            "crypto screener failed — keeping existing %d strategies", len(current_strategies)
        )
        return current_strategies

    all_symbols: list[str] = list(dict.fromkeys(screened + list(held_symbols)))

    # Reuse existing instances for symbols already in the universe — preserves
    # DonchianBreakout entry state across the daily rebuild.
    existing: dict[str, list] = {}
    for s in current_strategies:
        existing.setdefault(s.symbol, []).append(s)
    new_strategies: list = []
    new_count = 0
    for sym in all_symbols:
        if sym in existing:
            new_strategies.extend(existing[sym])
        else:
            new_strategies.extend(_build_crypto_strategies_for(config, [sym]))
            new_count += 1

    logger.info(
        "crypto universe refreshed: %d screened + %d held = %d symbols → %d strategies (%d new)",
        len(screened),
        len(held_symbols),
        len(all_symbols),
        len(new_strategies),
        new_count,
    )
    return new_strategies


if __name__ == "__main__":
    import threading

    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg.require_alpaca()
    if not cfg.database_url:
        raise RuntimeError("DATABASE_URL is required for standalone scheduler")
    _broker = AlpacaBroker(cfg)
    _repo = PostgresRepository(cfg.database_url)

    # Launch crypto scheduler in a background thread when crypto trading is enabled.
    # Dynamic mode: start empty — universe built on first tick.
    # Static mode: build from CRYPTO_ALLOWLIST.
    _run_crypto = cfg.risk.dynamic_crypto_universe or bool(cfg.risk.crypto_allowlist)
    if _run_crypto:
        if cfg.risk.dynamic_crypto_universe:
            _crypto_strategies: list = []
            logger.info("dynamic crypto universe mode — strategies built on first tick")
        else:
            _crypto_strategies = _build_crypto_strategies(cfg)
        _crypto_thread = threading.Thread(
            target=start_crypto_scheduler,
            args=(cfg, _crypto_strategies, _broker, _repo),
            daemon=True,
            name="crypto-scheduler",
        )
        _crypto_thread.start()
        logger.info("crypto scheduler thread started")

    # Dynamic mode: start with empty list — rebuilt on first market-open tick.
    # Static mode: build from allowlist (or DEFAULT_ALLOWLIST if unset).
    if cfg.risk.dynamic_universe:
        _strategies = []
        logger.info("dynamic universe mode — strategies built at market open each day")
    else:
        _strategies = _build_strategies_for(cfg, list(cfg.risk.allowlist or []))
    start_scheduler(cfg, _strategies, _broker, _repo)  # blocks main thread
