"""Calendar route — daily P&L tiles + per-day trade drilldown."""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter

from api.deps import get_broker, get_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar", tags=["calendar"])

_CACHE_TTL = 60
_cache: dict = {}


def _compute_sync(broker, repo) -> list[dict]:
    from trader.performance.calendar import compute_calendar_data
    return compute_calendar_data(broker, repo)


@router.get("")
async def get_calendar():
    now = time.monotonic()
    if _cache.get("computed_at", 0) + _CACHE_TTL > now:
        return _cache["result"]

    try:
        broker = get_broker()
        repo = get_repo()
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _compute_sync, broker, repo)
        _cache["result"] = data
        _cache["computed_at"] = now
        return data
    except Exception:
        logger.exception("calendar compute failed")
        return []
