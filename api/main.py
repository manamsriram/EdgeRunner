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

from api.auth import router as auth_router
from api.routes.analysis import router as analysis_router
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
    from trader.portfolio.sqlite_repo import SQLiteRepository
    from trader.scheduler import _build_default_strategies, run_once

    cfg = load_config()
    if not cfg.alpaca_api_key:
        logger.warning("ALPACA_API_KEY not set — scheduler disabled")
        return

    if cfg.database_url:
        from trader.portfolio.postgres_repo import PostgresRepository
        repo = PostgresRepository(cfg.database_url)
    else:
        repo = SQLiteRepository(cfg.portfolio_db_path)

    broker = AlpacaBroker(cfg)
    strategies = _build_default_strategies(cfg)
    loop = asyncio.get_event_loop()

    logger.info("scheduler loop started — autonomy=%s poll=60s", cfg.autonomy)
    while True:
        try:
            await loop.run_in_executor(None, run_once, cfg, strategies, broker, repo)
        except Exception:
            logger.exception("scheduler tick error")
        await asyncio.sleep(60)


async def _crypto_scheduler_loop() -> None:
    """Run the crypto trading scheduler every 5 minutes, 24/7.

    Skips silently if ALPACA_API_KEY or CRYPTO_ALLOWLIST is not configured.
    """
    from trader.config import load_config
    from trader.execution.broker import AlpacaBroker
    from trader.portfolio.sqlite_repo import SQLiteRepository
    from trader.scheduler import _build_crypto_strategies, run_once_crypto

    cfg = load_config()
    if not cfg.alpaca_api_key:
        return
    if not cfg.risk.crypto_allowlist:
        logger.info("CRYPTO_ALLOWLIST not set — crypto scheduler disabled")
        return

    if cfg.database_url:
        from trader.portfolio.postgres_repo import PostgresRepository
        repo = PostgresRepository(cfg.database_url)
    else:
        repo = SQLiteRepository(cfg.portfolio_db_path)

    broker = AlpacaBroker(cfg)
    strategies = _build_crypto_strategies(cfg)
    loop = asyncio.get_event_loop()

    logger.info("crypto scheduler loop started — autonomy=%s poll=240s symbols=%s", cfg.autonomy, list(cfg.risk.crypto_allowlist))
    while True:
        try:
            await loop.run_in_executor(None, run_once_crypto, cfg, strategies, broker, repo)
        except Exception:
            logger.exception("crypto scheduler tick error")
        await asyncio.sleep(240)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(proposal_poller())
    asyncio.create_task(_scheduler_loop())
    asyncio.create_task(_crypto_scheduler_loop())
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
app.include_router(auth_router)
app.include_router(proposals_router, prefix="/api")
app.include_router(portfolio_router, prefix="/api")
app.include_router(controls_router, prefix="/api")
app.include_router(analysis_router, prefix="/api")


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
