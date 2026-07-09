"""Analysis route: run stock analysis agent in a subprocess, stream result via SSE.

The agent lives in tools/fetch_stock_info.py, which imports LangChain + yfinance +
openai (~100-200MB resident). Running it in-process on Render's 512MB tier caused
OOM restarts — a subprocess returns all of that memory to the OS when it exits.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps import save_query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])

ANALYSIS_TIMEOUT_S = 120.0

# One analysis child at a time — two concurrent LangChain subprocesses would blow
# the 512MB container cap.
_analysis_semaphore = asyncio.Semaphore(1)

# Per-IP rate limit: endpoint is public and each call is a real LLM spend, so cap
# how often any one caller can trigger it. In-memory fixed window — fine for a
# single-process deploy; ponytail: not shared across workers/restarts, move to
# Postgres/Redis if this ever runs with >1 worker.
_RATE_LIMIT = 5           # requests
_RATE_WINDOW_S = 600      # per 10 minutes
_hits: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    hits = _hits[ip]
    while hits and now - hits[0] > _RATE_WINDOW_S:
        hits.popleft()
    if len(hits) >= _RATE_LIMIT:
        retry_after = int(_RATE_WINDOW_S - (now - hits[0])) + 1
        raise HTTPException(
            status_code=429,
            detail=f"rate limit exceeded — max {_RATE_LIMIT} analyses per {_RATE_WINDOW_S // 60} min",
            headers={"Retry-After": str(retry_after)},
        )
    hits.append(now)


class AnalysisRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


def _run_analysis_subprocess(query: str) -> str:
    """Run the analysis agent in a child process; returns the result text.

    Raises subprocess.TimeoutExpired on timeout (child is killed) and
    RuntimeError on a non-zero exit.
    """
    with tempfile.NamedTemporaryFile(mode="r", suffix=".txt", delete=True) as result_file:
        proc = subprocess.run(
            [sys.executable, "-m", "tools.fetch_stock_info", query, result_file.name],
            capture_output=True,
            text=True,
            timeout=ANALYSIS_TIMEOUT_S,
            cwd=Path(__file__).resolve().parents[2],
        )
        if proc.returncode != 0:
            logger.error("analysis subprocess exited %d: %s", proc.returncode, proc.stderr[-2000:])
            raise RuntimeError(f"analysis subprocess exited {proc.returncode}")
        return result_file.read()


@router.post("")
async def run_analysis(body: AnalysisRequest, request: Request):
    """Run the stock analysis agent in a subprocess (non-blocking). Public endpoint —
    one query at a time (see `_analysis_semaphore`) bounds cost/memory regardless of caller,
    and `_check_rate_limit` caps how often any one IP can trigger a (paid) LLM call.

    Returns an SSE stream: first a `chunk` event with the full response, then a `done`
    event. The frontend reads this via fetch() + ReadableStream.
    """
    _check_rate_limit(_client_ip(request))

    async def event_stream():
        loop = asyncio.get_event_loop()
        try:
            async with _analysis_semaphore:
                result: str = await loop.run_in_executor(
                    None, _run_analysis_subprocess, body.query
                )
            try:
                save_query("public", body.query, result)
            except Exception:
                logger.warning("failed to save query")
            yield f"data: {json.dumps({'chunk': result})}\n\n"
        except subprocess.TimeoutExpired:
            logger.warning("analysis timed out")
            yield f"data: {json.dumps({'error': 'analysis timed out after 120s'})}\n\n"
        except Exception:
            logger.exception("analysis failed")
            yield f"data: {json.dumps({'error': 'analysis failed'})}\n\n"
        finally:
            yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if behind proxy
        },
    )
