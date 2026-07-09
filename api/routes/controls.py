"""Controls routes: kill switch, autonomy mode, run log."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from api.deps import get_config, get_current_user, get_repo

router = APIRouter(prefix="/controls", tags=["controls"])

_autonomy_override: str | None = None


def _kill_switch():
    from trader.risk.gate import KillSwitch
    return KillSwitch(get_config().kill_switch_path)


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
    mode = _autonomy_override if _autonomy_override is not None else get_config().autonomy
    return {"mode": mode}


class AutonomyRequest(BaseModel):
    mode: Literal["manual", "auto"]


@router.post("/autonomy")
def set_autonomy_mode(body: AutonomyRequest, username: str = Depends(get_current_user)):
    global _autonomy_override
    _autonomy_override = body.mode
    logger.info("autonomy mode set to %s by %s", body.mode, username)
    return {"mode": _autonomy_override}


@router.get("/runs")
def run_log(username: str = Depends(get_current_user)):
    try:
        return get_repo().get_runs()
    except Exception:
        logger.exception("failed to fetch run log")
        raise HTTPException(status_code=500, detail="run log unavailable; see server logs")
