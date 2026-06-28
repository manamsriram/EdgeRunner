"""Analysis route: run stock analysis agent, stream result via SSE."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps import get_current_user, save_query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])


class AnalysisRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


@router.post("")
async def run_analysis(body: AnalysisRequest, username: str = Depends(get_current_user)):
    """Run the LangChain stock analysis agent in a thread pool (non-blocking).

    Returns an SSE stream: first a `chunk` event with the full response, then a `done`
    event. The frontend reads this via fetch() + ReadableStream.
    """

    async def event_stream():
        loop = asyncio.get_event_loop()
        try:
            from tools.fetch_stock_info import Analyze_stock

            result: str = await asyncio.wait_for(
                loop.run_in_executor(None, Analyze_stock, body.query),
                timeout=120.0,
            )
            try:
                save_query(username, body.query, result)
            except Exception:
                logger.warning("failed to save query for %s", username)
            yield f"data: {json.dumps({'chunk': result})}\n\n"
        except asyncio.TimeoutError:
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
