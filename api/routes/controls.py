"""Controls routes: kill switch, autonomy mode, run log."""
from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)

from api.deps import get_config, get_current_user

router = APIRouter(prefix="/controls", tags=["controls"])


def _kill_switch():
    from trader.risk.gate import KillSwitch
    return KillSwitch(get_config().kill_switch_path)


@router.get("/kill-switch")
def kill_switch_status(username: str = Depends(get_current_user)):
    ks = _kill_switch()
    return {"engaged": ks.engaged()}


@router.post("/kill-switch/engage")
def engage_kill_switch(username: str = Depends(get_current_user)):
    _kill_switch().engage("dashboard")
    return {"engaged": True}


@router.post("/kill-switch/disengage")
def disengage_kill_switch(username: str = Depends(get_current_user)):
    _kill_switch().disengage()
    return {"engaged": False}


@router.get("/autonomy")
def autonomy_mode(username: str = Depends(get_current_user)):
    return {"mode": get_config().autonomy}


@router.get("/runs")
def run_log(username: str = Depends(get_current_user)):
    cfg = get_config()
    try:
        conn = sqlite3.connect(cfg.portfolio_db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            rows = conn.execute(
                "SELECT id, started_at, strategy, mode, note FROM runs "
                "ORDER BY id DESC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("failed to fetch run log")
        raise HTTPException(status_code=500, detail="run log unavailable; see server logs")
