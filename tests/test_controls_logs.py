"""Journal-tail endpoint: auth, unit allowlisting, and absent-journalctl behaviour."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.controls import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    from api.deps import get_current_user

    app.dependency_overrides[get_current_user] = lambda: "test@example.com"
    return TestClient(app)


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_rejects_unit_outside_allowlist(client):
    """journalctl -u takes a glob, so an unvalidated unit reads any log on the box."""
    resp = client.get("/api/controls/logs", params={"unit": "ssh"})
    assert resp.status_code == 400

    resp = client.get("/api/controls/logs", params={"unit": "*"})
    assert resp.status_code == 400


def test_returns_journal_lines(client):
    with patch("shutil.which", return_value="/usr/bin/journalctl"), patch(
        "subprocess.run", return_value=_completed("line one\nline two\n")
    ) as run:
        resp = client.get("/api/controls/logs")

    assert resp.status_code == 200
    assert resp.json() == {"unit": "edgerunner", "lines": ["line one", "line two"]}
    assert "--no-pager" in run.call_args[0][0]


def test_deploy_timer_unit_is_readable(client):
    """The deploy poller is its own systemd unit; its log must be reachable too."""
    with patch("shutil.which", return_value="/usr/bin/journalctl"), patch(
        "subprocess.run", return_value=_completed("")
    ) as run:
        resp = client.get("/api/controls/logs", params={"unit": "edgerunner-deploy"})

    assert resp.status_code == 200
    argv = run.call_args[0][0]
    assert argv[argv.index("-u") + 1] == "edgerunner-deploy"


def test_503_when_journalctl_missing(client):
    """Render has no systemd; say so rather than returning an empty log."""
    with patch("shutil.which", return_value=None):
        resp = client.get("/api/controls/logs")
    assert resp.status_code == 503


def test_lines_bounded(client):
    assert client.get("/api/controls/logs", params={"lines": 5000}).status_code == 422
    assert client.get("/api/controls/logs", params={"lines": 0}).status_code == 422


def test_requires_auth():
    """No dependency override: with auth on, a tokenless request must 401.

    Asserted before journalctl is even looked up — an unauthenticated caller must not
    learn whether this host has a journal, let alone read it.
    """
    app = FastAPI()
    app.include_router(router, prefix="/api")
    with patch("api.deps.auth_enabled", return_value=True):
        resp = TestClient(app).get("/api/controls/logs")
    assert resp.status_code == 401
