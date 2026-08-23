"""Portfolio routes: positions, orders, portfolio history."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)

from api.deps import get_broker, get_repo, require_read_auth

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

_CACHE_TTL = 30  # seconds; matches frontend poll cadence, cuts redundant broker round-trips
_cache: dict = {}


def _cached(key: str, compute):
    now = time.monotonic()
    entry = _cache.get(key)
    if entry and entry[0] + _CACHE_TTL > now:
        return entry[1]
    result = compute()
    _cache[key] = (now, result)
    return result


@router.get("/positions")
def positions(_user: str | None = Depends(require_read_auth)):
    try:
        return _cached("positions", lambda: get_broker().get_positions())
    except Exception:
        logger.exception("failed to fetch positions")
        raise HTTPException(status_code=502, detail="could not fetch positions; see server logs")


@router.get("/orders")
def orders(_user: str | None = Depends(require_read_auth)):
    return get_repo().get_orders(limit=50)


@router.get("/history")
def portfolio_history(_user: str | None = Depends(require_read_auth)):
    history = _cached("history", lambda: get_broker().get_portfolio_history())
    if history is None:
        return {"timestamp": [], "equity": []}
    return history
