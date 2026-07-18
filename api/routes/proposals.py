"""Proposals routes: list pending, approve, reject."""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)

from api.deps import get_broker, get_config, get_current_user, get_repo
from api.proposals_cache import get_pending, invalidate
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
    """Fetch a pending proposal by id; used for reject (low race risk)."""
    repo = get_repo()
    for p in repo.list_pending_proposals():
        if p["id"] == proposal_id:
            return p
    return None


@router.get("")
def list_proposals(username: str = Depends(get_current_user)):
    # Read through the shared TTL cache so the dashboard's REST refresh and the
    # WS poller's tick collapse into a single DB query per 10 s window.
    return get_pending(get_repo())


@router.post("/{proposal_id}/approve")
def approve(proposal_id: int, username: str = Depends(get_current_user)):
    repo = get_repo()
    broker = get_broker()
    config = get_config()

    # Atomic claim: UPDATE WHERE status='pending' — rejects concurrent duplicates.
    proposal = repo.try_approve_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=409, detail="proposal already resolved or not found")
    # State changed out of PENDING — drop the cached pending list so the WS poller
    # and any concurrent REST reader see fresh state.
    invalidate()

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
                invalidate()  # rolled back to PENDING — re-show in list
                raise HTTPException(status_code=422, detail="cannot submit sell: ref_price is zero")
            # Cancel any broker-side stop order before closing the position.
            try:
                broker.cancel_open_stops(proposal["symbol"])
            except Exception:
                logger.warning("cancel_open_stops failed for %s; proceeding with sell", proposal["symbol"])
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
            # Place a GTC stop order to protect the new long position.
            ref_price = proposal["ref_price"]
            if ref_price and ref_price > 0:
                from trader.risk.gate import is_crypto_symbol
                if not is_crypto_symbol(proposal["symbol"]):
                    stop_price = ref_price * (1 - config.risk.stop_loss_pct)
                    stop_qty = proposal["notional"] / ref_price
                    stop_coid = client_order_id_for(
                        trade_date, proposal["symbol"], "sell", f"stop-proposal-{proposal_id}"
                    )
                    try:
                        broker.place_stop_order(
                            symbol=proposal["symbol"],
                            qty=stop_qty,
                            stop_price=stop_price,
                            client_order_id=stop_coid,
                        )
                        logger.info(
                            "placed GTC stop for %s at %.2f", proposal["symbol"], stop_price
                        )
                    except Exception:
                        logger.warning(
                            "broker stop order failed for %s — software stop remains active",
                            proposal["symbol"],
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
        invalidate()  # belt-and-suspenders; cache is already empty but racey
                      # cross-process reads (if we ever go multi-worker) need this.
        return {"status": "executed", "proposal_id": proposal_id}

    except HTTPException:
        raise
    except Exception:
        logger.exception("broker submission failed for proposal %s", proposal_id)
        repo.set_proposal_status(proposal_id, PROPOSAL_PENDING)
        invalidate()  # rolled back to PENDING — re-show in list
        raise HTTPException(status_code=502, detail="broker submission failed; see server logs")


@router.post("/{proposal_id}/reject")
def reject(proposal_id: int, username: str = Depends(get_current_user)):
    repo = get_repo()
    # Existence check reads the DB directly (not the cache): we want to catch a
    # proposal that was just approved by a concurrent request even if our cache
    # is stale. _get_pending_proposal already does this.
    if _get_pending_proposal(proposal_id) is None:
        raise HTTPException(status_code=409, detail="proposal already resolved or not found")
    repo.set_proposal_status(proposal_id, PROPOSAL_REJECTED)
    invalidate()
    return {"status": "rejected", "proposal_id": proposal_id}
