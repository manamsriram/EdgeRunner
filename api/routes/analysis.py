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
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps import get_current_user, save_query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])

ANALYSIS_TIMEOUT_S = 120.0

# One analysis child at a time — two concurrent LangChain subprocesses would blow
# the 512MB container cap.
_analysis_semaphore = asyncio.Semaphore(1)


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
async def run_analysis(body: AnalysisRequest, username: str = Depends(get_current_user)):
    """Run the stock analysis agent in a subprocess (non-blocking).

    Returns an SSE stream: first a `chunk` event with the full response, then a `done`
    event. The frontend reads this via fetch() + ReadableStream.
    """

    async def event_stream():
        loop = asyncio.get_event_loop()
        try:
            async with _analysis_semaphore:
                result: str = await loop.run_in_executor(
                    None, _run_analysis_subprocess, body.query
                )
            try:
                save_query(username, body.query, result)
            except Exception:
                logger.warning("failed to save query for %s", username)
            yield f"data: {json.dumps({'chunk': result})}\n\n"
        except subprocess.TimeoutExpired:
            logger.warning("analysis timed out for user %s", username)
            yield f"data: {json.dumps({'error': 'analysis timed out after 120s'})}\n\n"
        except Exception:
            logger.exception("analysis failed for user %s", username)
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
