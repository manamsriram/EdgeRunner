"""Proposals routes: list pending, approve, reject."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)

from api.deps import get_broker, get_config, get_current_user, get_repo
from trader.execution.broker import client_order_id_for
from trader.portfolio.repository import (
    PROPOSAL_APPROVED,
    PROPOSAL_EXECUTED,
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
    OrderRow,
)

router = APIRouter(prefix="/proposals", tags=["proposals"])


def _get_pending_proposal(proposal_id: int) -> dict | None:
    """Fetch a single proposal by id; returns None if not found."""
    repo = get_repo()
    all_pending = repo.list_pending_proposals()
    for p in all_pending:
        if p["id"] == proposal_id:
            return p
    return None


@router.get("")
def list_proposals(username: str = Depends(get_current_user)):
    return get_repo().list_pending_proposals()


@router.post("/{proposal_id}/approve")
def approve(proposal_id: int, username: str = Depends(get_current_user)):
    repo = get_repo()
    broker = get_broker()

    proposal = _get_pending_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=409, detail="proposal already resolved or not found")

    # Mark approved before submitting to Alpaca — rollback to pending on any failure
    repo.set_proposal_status(proposal_id, PROPOSAL_APPROVED)
    try:
        created_at = datetime.fromisoformat(str(proposal["created_at"]))
        trade_date = created_at.date()
        coid = client_order_id_for(
            trade_date, proposal["symbol"], proposal["side"], f"proposal-{proposal_id}"
        )

        if proposal["side"] == "sell":
            ref_price = proposal["ref_price"]
            if not ref_price:
                repo.set_proposal_status(proposal_id, PROPOSAL_PENDING)
                raise HTTPException(status_code=422, detail="cannot submit sell: ref_price is zero")
            qty = proposal["notional"] / ref_price
            order = broker.submit(
                symbol=proposal["symbol"],
                side=proposal["side"],
                client_order_id=coid,
                qty=qty,
            )
        else:
            order = broker.submit(
                symbol=proposal["symbol"],
                side=proposal["side"],
                client_order_id=coid,
                notional=proposal["notional"],
            )

        repo.record_order(OrderRow(
            client_order_id=coid,
            symbol=proposal["symbol"],
            side=proposal["side"],
            notional=proposal["notional"],
            status="submitted",
            broker_order_id=str(getattr(order, "id", "") or "") or None,
        ))
        repo.set_proposal_status(proposal_id, PROPOSAL_EXECUTED)
        return {"status": "executed", "proposal_id": proposal_id}

    except HTTPException:
        raise
    except Exception:
        logger.exception("broker submission failed for proposal %s", proposal_id)
        repo.set_proposal_status(proposal_id, PROPOSAL_PENDING)
        raise HTTPException(status_code=502, detail="broker submission failed; see server logs")


@router.post("/{proposal_id}/reject")
def reject(proposal_id: int, username: str = Depends(get_current_user)):
    repo = get_repo()
    if _get_pending_proposal(proposal_id) is None:
        raise HTTPException(status_code=409, detail="proposal already resolved or not found")
    repo.set_proposal_status(proposal_id, PROPOSAL_REJECTED)
    return {"status": "rejected", "proposal_id": proposal_id}
