"""Controls routes: kill switch, autonomy mode, run log."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from api.deps import get_config, get_current_user, get_repo

router = APIRouter(prefix="/controls", tags=["controls"])


def _kill_switch():
    from trader.risk.gate import KillSwitch
    return KillSwitch(get_config().kill_switch_path)


def _autonomy_override():
    from trader.risk.gate import AutonomyOverride
    return AutonomyOverride(get_config().autonomy_override_path)


@router.get("/kill-switch")
def kill_switch_status(username: str = Depends(get_current_user)):
    ks = _kill_switch()
    return {"engaged": ks.engaged(), "note": ks.note()}


@router.post("/kill-switch/engage")
def engage_kill_switch(username: str = Depends(get_current_user)):
    from datetime import datetime, timezone

    note = f"dashboard by {username} at {datetime.now(timezone.utc).isoformat()}"
    _kill_switch().engage(note)
    logger.warning("kill switch engaged by %s", username)
    return {"engaged": True}


@router.post("/kill-switch/disengage")
def disengage_kill_switch(username: str = Depends(get_current_user)):
    _kill_switch().disengage()
    logger.warning("kill switch disengaged by %s", username)
    return {"engaged": False}


@router.get("/autonomy")
def autonomy_mode(username: str = Depends(get_current_user)):
    from trader.risk.gate import effective_autonomy
    return {"mode": effective_autonomy(get_config())}


class AutonomyRequest(BaseModel):
    mode: Literal["manual", "auto"]


@router.post("/autonomy")
def set_autonomy_mode(
    body: AutonomyRequest, request: Request, username: str = Depends(get_current_user)
):
    # File-backed so the running scheduler/pipeline actually reads it (a module
    # global would only change what this API process reports, not trading behaviour).
    _autonomy_override().set(body.mode)
    # Log the stable JWT subject id, not the email get_current_user returns (avoid PII in logs).
    logger.warning("autonomy mode set to %s by %s", body.mode, request.state.auth_sub)
    return {"mode": body.mode}


@router.get("/runs")
def run_log(username: str = Depends(get_current_user)):
    try:
        return get_repo().get_runs()
    except Exception:
        logger.exception("failed to fetch run log")
        raise HTTPException(status_code=500, detail="run log unavailable; see server logs")
