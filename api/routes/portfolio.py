"""Portfolio routes: positions, orders, portfolio history."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

from api.deps import get_broker, get_repo

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/positions")
def positions():
    try:
        return get_broker().get_positions()
    except Exception:
        logger.exception("failed to fetch positions")
        raise HTTPException(status_code=502, detail="could not fetch positions; see server logs")


@router.get("/orders")
def orders():
    all_orders = get_repo().get_orders()
    return all_orders[-50:]  # most recent 50


@router.get("/history")
def portfolio_history():
    history = get_broker().get_portfolio_history()
    if history is None:
        return {"timestamp": [], "equity": []}
    return history
