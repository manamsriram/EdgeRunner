"""Market-hours scheduler using Alpaca's clock API.

Run as a blocking process from the command line:
    python -m trader.scheduler

The Streamlit dashboard runs separately (shares the same SQLite DB via WAL mode).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from trader.alerts import send_alert
from trader.config import Config, load_config
from trader.execution.broker import AlpacaBroker
from trader.pipeline import PipelineRun, run_pipeline
from trader.portfolio.repository import PortfolioRepository
from trader.portfolio.sqlite_repo import SQLiteRepository
from trader.risk.gate import KillSwitch

if TYPE_CHECKING:
    from trader.strategy.base import Strategy

logger = logging.getLogger(__name__)


def is_market_open(broker: AlpacaBroker) -> bool:
    """Return True if Alpaca says the market is currently open.

    Fails closed: any API error returns False so the scheduler does not trade
    during an outage or misconfiguration.
    """
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
            send_alert("Kill switch engaged — trading halted", config.slack_webhook_url)
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

    Runs until interrupted (KeyboardInterrupt) or killed.
    """
    logger.info(
        "scheduler starting — autonomy=%s poll=%dm symbols=%s",
        config.autonomy,
        poll_minutes,
        [s.symbol for s in strategies],
    )
    while True:
        try:
            results = run_once(config, strategies, broker, repo)
            for r in results:
                logger.info("tick result: symbol=%s outcome=%s", r.symbol, r.outcome)
        except Exception:
            logger.exception("unhandled error in scheduler tick — continuing")
        time.sleep(poll_minutes * 60)


def _build_default_strategies(config: Config) -> "list[Strategy]":
    """Build one strategy instance per allowlist symbol using the default strategy."""
    from trader.strategy.ma_crossover import MACrossover
    return [MACrossover(symbol=sym) for sym in config.risk.allowlist]


if __name__ == "__main__":
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg.require_alpaca()
    _broker = AlpacaBroker(cfg)
    _repo = SQLiteRepository(cfg.portfolio_db_path)
    _strategies = _build_default_strategies(cfg)
    start_scheduler(cfg, _strategies, _broker, _repo)
