"""Controls routes: kill switch, autonomy mode, run log, journal tail."""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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


# Only units this box actually owns. Not a caller-supplied name: journalctl -u takes a
# glob, so an unvalidated value would read any unit's log through an authenticated
# endpoint.
_JOURNAL_UNITS = {"edgerunner", "caddy"}
_JOURNAL_TAGS = {"edgerunner-deploy"}


@router.get("/logs")
def journal_tail(
    username: str = Depends(get_current_user),
    unit: str = Query("edgerunner"),
    lines: int = Query(200, ge=1, le=2000),
    since: str | None = Query(None, description="journalctl --since value, e.g. '1 hour ago'"),
):
    """Tail this host's systemd journal — the logs SSH would show, over the API.

    Covers what /controls/runs cannot: tracebacks, OOM kills, systemd restarts, and
    nightly deploy output. Journald-only, so it returns 503 on Render (no systemd)
    rather than pretending to be empty.
    """
    if unit not in _JOURNAL_UNITS and unit not in _JOURNAL_TAGS:
        raise HTTPException(status_code=400, detail=f"unknown unit '{unit}'")

    journalctl = shutil.which("journalctl")
    if not journalctl:
        raise HTTPException(status_code=503, detail="journalctl unavailable on this host")

    selector = ["-t", unit] if unit in _JOURNAL_TAGS else ["-u", unit]
    cmd = [journalctl, *selector, "-n", str(lines), "--no-pager", "-o", "short-iso"]
    if since:
        cmd += ["--since", since]

    try:
        # No shell: argv is fixed and `unit` is allowlisted above.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="journalctl timed out")

    if proc.returncode != 0:
        # Most likely cause is the service user lacking systemd-journal group
        # membership, which journald reports as an empty read rather than a hard
        # error — surface stderr so that is diagnosable without SSH.
        logger.error("journalctl failed (rc=%s): %s", proc.returncode, proc.stderr.strip())
        raise HTTPException(status_code=500, detail="journal read failed; see server logs")

    return {"unit": unit, "lines": proc.stdout.splitlines()}
