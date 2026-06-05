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

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(proposal_poller())
    logger.info("proposal poller started")
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
