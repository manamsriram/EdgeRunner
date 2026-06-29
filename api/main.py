"""FastAPI application — assembles all routers and serves the React SPA."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from api.routes.analysis import router as analysis_router
from api.routes.calendar import router as calendar_router
from api.routes.performance import router as performance_router
from api.routes.controls import router as controls_router
from api.routes.portfolio import router as portfolio_router
from api.routes.proposals import router as proposals_router
from api.ws import proposal_poller, ws_handler

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


async def _scheduler_loop() -> None:
    """Run the trading scheduler in a thread pool every 60 s.

    Blocking broker/data calls run via run_in_executor so they don't stall
    the FastAPI event loop. Skips silently if ALPACA_API_KEY is not configured.
    """
    from trader.config import load_config
    from trader.execution.broker import AlpacaBroker
    from trader.portfolio.postgres_repo import PostgresRepository
    from trader.scheduler import _build_strategies_for, _build_intraday_strategies_for, run_once

    cfg = load_config()
    if not cfg.alpaca_api_key:
        logger.warning("ALPACA_API_KEY not set — scheduler disabled")
        return
    if not cfg.database_url:
        logger.error("DATABASE_URL not set — equity scheduler disabled")
        return

    loop = asyncio.get_running_loop()
    repo = await loop.run_in_executor(None, PostgresRepository, cfg.database_url)

    broker = AlpacaBroker(cfg)
    broker.start_trade_stream()

    _raw_intraday = os.getenv("INTRADAY_ALLOWLIST", "").strip()
    _intraday_symbols = [s.strip().upper() for s in _raw_intraday.split(",") if s.strip()]
    _intraday_strategies = _build_intraday_strategies_for(_intraday_symbols)

    if cfg.risk.dynamic_universe:
        symbols: list[str] = []
        strategies = []
        logger.info("equity scheduler started — dynamic universe mode, strategies built on first tick poll=60s")
    else:
        symbols = list(cfg.risk.allowlist or [])
        _overlap = set(_intraday_symbols) & set(symbols)
        if _overlap:
            logger.warning(
                "INTRADAY_ALLOWLIST overlaps daily universe: %s — overlapping symbols share broker positions",
                _overlap,
            )
        strategies = _build_strategies_for(cfg, symbols) + _intraday_strategies
        logger.info("equity scheduler started — autonomy=%s poll=60s symbols=%s", cfg.autonomy, symbols)

    from datetime import date as _date
    universe_date = None  # always None at start — loop fetches on first tick for dynamic mode
    signal_precomputed_date = None
    while True:
        try:
            if cfg.risk.dynamic_universe:
                from datetime import date as _date
                from trader.scheduler import _refresh_dynamic_universe
                today = _date.today()
                first_run = universe_date is None
                if universe_date != today and (first_run or today.weekday() == 0):
                    strategies = await loop.run_in_executor(
                        None, _refresh_dynamic_universe, cfg, broker, strategies
                    )
                    strategies = strategies + _intraday_strategies  # re-append after refresh
                    universe_date = today
            from trader.scheduler import is_market_open as _is_open
            if not await loop.run_in_executor(None, _is_open, broker):
                from datetime import date as _date, datetime as _dt, timezone as _tz
                today = _date.today()
                if signal_precomputed_date != today:
                    from trader.pipeline import precompute_signals
                    await loop.run_in_executor(
                        None, precompute_signals, cfg, strategies, _dt.now(_tz.utc)
                    )
                    signal_precomputed_date = today
            await loop.run_in_executor(None, run_once, cfg, strategies, broker, repo)
        except Exception:
            logger.exception("scheduler tick error")
        await asyncio.sleep(60)


async def _crypto_scheduler_loop() -> None:
    """Run the crypto trading scheduler every 5 minutes, 24/7.

    Skips silently if ALPACA_API_KEY is not set and neither CRYPTO_ALLOWLIST
    nor DYNAMIC_CRYPTO_UNIVERSE is configured.
    """
    from trader.config import load_config
    from trader.execution.broker import AlpacaBroker
    from trader.portfolio.postgres_repo import PostgresRepository
    from trader.scheduler import _build_crypto_strategies, _build_crypto_strategies_for, run_once_crypto

    cfg = load_config()
    if not cfg.alpaca_api_key:
        return
    if not cfg.risk.crypto_allowlist and not cfg.risk.dynamic_crypto_universe:
        logger.info("CRYPTO_ALLOWLIST not set and DYNAMIC_CRYPTO_UNIVERSE disabled — crypto scheduler disabled")
        return
    if not cfg.database_url:
        logger.error("DATABASE_URL not set — crypto scheduler disabled")
        return

    loop = asyncio.get_running_loop()
    repo = await loop.run_in_executor(None, PostgresRepository, cfg.database_url)

    broker = AlpacaBroker(cfg)

    if cfg.risk.dynamic_crypto_universe:
        from trader.universe.crypto_screener import fetch_dynamic_crypto_universe
        symbols = None
        for attempt in range(1, 4):
            try:
                symbols = await loop.run_in_executor(
                    None, fetch_dynamic_crypto_universe, cfg, cfg.risk.crypto_universe_size
                )
                break
            except Exception:
                logger.warning("crypto screener attempt %d/3 failed", attempt, exc_info=True)
                if attempt < 3:
                    await asyncio.sleep(60)
        if symbols is None:
            fallback = list(cfg.risk.crypto_allowlist) or ["BTC/USD", "ETH/USD"]
            logger.warning("crypto screener failed 3 times — falling back to %s", fallback)
            symbols = fallback
        strategies = _build_crypto_strategies_for(cfg, symbols)
        logger.info("crypto scheduler started — dynamic universe size=%d poll=240s", len(symbols))
    else:
        strategies = _build_crypto_strategies(cfg)
        logger.info("crypto scheduler loop started — autonomy=%s poll=240s symbols=%s", cfg.autonomy, list(cfg.risk.crypto_allowlist))

    from datetime import date as _date
    crypto_universe_date = _date.today() if cfg.risk.dynamic_crypto_universe else None
    while True:
        try:
            if cfg.risk.dynamic_crypto_universe:
                from datetime import date as _date
                from trader.scheduler import _refresh_dynamic_crypto_universe
                today = _date.today()
                first_run = crypto_universe_date is None
                if crypto_universe_date != today and (first_run or today.weekday() == 0):
                    strategies = await loop.run_in_executor(
                        None, _refresh_dynamic_crypto_universe, cfg, broker, strategies
                    )
                    crypto_universe_date = today
            await loop.run_in_executor(None, run_once_crypto, cfg, strategies, broker, repo)
        except Exception:
            logger.exception("crypto scheduler tick error")
        await asyncio.sleep(240)


def _run_migrations() -> None:
    """Apply pending Alembic migrations. Skipped if DATABASE_URL is not set.

    Requires a session-mode pooler or direct connection — transaction-mode pooler
    (PgBouncer port 6543) does not support the DDL transactions Alembic uses.
    Use Supabase session pooler (port 5432) or set MIGRATION_DATABASE_URL to override.
    """
    db_url = os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        logger.warning("DATABASE_URL not set — skipping migrations")
        return
    # Add connect_timeout so a blocked/slow DB doesn't hang startup indefinitely.
    if "connect_timeout" not in db_url:
        sep = "&" if "?" in db_url else "?"
        db_url = f"{db_url}{sep}connect_timeout=10"
    from pathlib import Path
    from alembic.config import Config as AlembicConfig
    from alembic import command as alembic_command
    ini_path = Path(__file__).parent.parent / "alembic.ini"
    cfg = AlembicConfig(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    try:
        alembic_command.upgrade(cfg, "head")
    except Exception:
        logging.getLogger().setLevel(logging.INFO)
        logger.exception("alembic upgrade failed")
        raise
    finally:
        # env.py calls fileConfig(alembic.ini) which resets root logger to WARNING.
        # Restore so all subsequent app INFO logs are visible.
        logging.getLogger().setLevel(logging.INFO)
    logger.info("database migrations applied")


async def _guarded(coro, name: str):
    try:
        await coro
    except Exception:
        logger.exception("%s crashed", name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _run_migrations),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        logger.error("migrations timed out after 30s — continuing startup without migrations")
    except Exception:
        logger.exception("migrations failed — continuing startup")
    asyncio.create_task(_guarded(proposal_poller(), "proposal_poller"))
    asyncio.create_task(_guarded(_scheduler_loop(), "equity_scheduler"))
    asyncio.create_task(_guarded(_crypto_scheduler_loop(), "crypto_scheduler"))
    logger.info("proposal poller, equity scheduler, and crypto scheduler started")
    yield


app = FastAPI(title="Stock Analyzer Bot API", version="1.0.0", lifespan=lifespan)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(_SecurityHeadersMiddleware)

# ---- CORS (dev only — React dev server on port 5173) ----
_FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- API routers ----
app.include_router(proposals_router, prefix="/api")
app.include_router(portfolio_router, prefix="/api")
app.include_router(controls_router, prefix="/api")
app.include_router(analysis_router, prefix="/api")
app.include_router(performance_router, prefix="/api")
app.include_router(calendar_router, prefix="/api")


# ---- WebSocket ----
@app.websocket("/ws/updates")
async def websocket_endpoint(websocket: WebSocket):
    await ws_handler(websocket)


# ---- Health check (Render pings GET /) ----
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def health():
    return {"status": "ok"}


# ---- SPA static files (production) ----
_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _DIST.exists():
    # Mount assets so /assets/* are served directly (before the catch-all)
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    # Catch-all: serve index.html for any non-API path so React Router handles routing.
    # IMPORTANT: this route must be defined AFTER all API routers.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_catchall(full_path: str):
        index = _DIST / "index.html"
        return FileResponse(str(index))
else:
    logger.info(
        "frontend/dist not found — run `cd frontend && npm run build` to enable SPA serving"
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        reload=True,
        reload_excludes=["venv", ".git", "frontend/node_modules"],
    )
