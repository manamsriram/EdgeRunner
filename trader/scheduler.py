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
from trader.portfolio.sqlite_repo import SQLiteRepository
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

    while True:
        try:
            # Daily universe refresh — runs once per calendar day on the first open tick.
            if config.risk.dynamic_universe and is_market_open(broker):
                today = date.today()
                if universe_date != today:
                    current_strategies = _refresh_dynamic_universe(
                        config, broker, current_strategies
                    )
                    universe_date = today

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
                if crypto_universe_date != today:
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
    """Build 5-strategy stack per equity symbol.

    SuperTrend (ATR-adaptive trend, ADX-filtered) replaces MACrossover.
    EquityBollingerReversion adds a mean-reversion signal anti-correlated with trend
    strategies. DonchianBreakout captures channel breakouts without the one-day
    entry delay that degraded GapPatternA. DipRecovery buys >=10% drawdowns from
    the all-time high and exits 5% above it — backtests (2yr and 4yr, split-adjusted)
    showed the best Sharpe of any strategy and improved the combo on both windows.
    """
    from trader.strategy.supertrend import SuperTrend
    from trader.strategy.smash_day import SmashDayB
    from trader.strategy.equity_reversion import EquityBollingerReversion
    from trader.strategy.donchian_breakout import DonchianBreakout
    from trader.strategy.dip_recovery import DipRecovery

    strategies: list[Strategy] = []
    for sym in symbols:
        strategies.append(SuperTrend(symbol=sym))
        strategies.append(SmashDayB(symbol=sym, long_only=True))
        strategies.append(EquityBollingerReversion(symbol=sym))
        strategies.append(DonchianBreakout(symbol=sym))
        strategies.append(DipRecovery(symbol=sym))
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
    new_strategies = _build_strategies_for(config, all_symbols)
    logger.info(
        "universe refreshed: %d screened + %d held = %d symbols → %d strategies",
        len(screened),
        len(held_symbols),
        len(all_symbols),
        len(new_strategies),
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
    new_strategies = _build_crypto_strategies_for(config, all_symbols)
    logger.info(
        "crypto universe refreshed: %d screened + %d held = %d symbols → %d strategies",
        len(screened),
        len(held_symbols),
        len(all_symbols),
        len(new_strategies),
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
    _broker = AlpacaBroker(cfg)
    _repo = SQLiteRepository(cfg.portfolio_db_path)

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
