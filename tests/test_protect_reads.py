"""PROTECT_READS gates the read-only routes on the live-money deployment.

Paper stays public on purpose (fake money); the GCP live box sets PROTECT_READS=true
so positions, order flow and the equity curve need a valid Supabase JWT. Both
deployments run this same code, so the flag is the only thing separating them —
which is exactly why it needs a test.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.deps as deps


def _client(monkeypatch, protect: str) -> TestClient:
    # auth_enabled() must be True or get_current_user short-circuits to "admin".
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "x" * 40)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("PROTECT_READS", protect)
    deps.get_config.cache_clear()
    deps._protect_reads.cache_clear()

    app = FastAPI()
    for mod in ("api.routes.calendar", "api.routes.portfolio", "api.routes.performance"):
        app.include_router(importlib.reload(importlib.import_module(mod)).router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


READ_PATHS = [
    "/api/calendar",
    "/api/portfolio/positions",
    "/api/portfolio/orders",
    "/api/portfolio/history",
    "/api/performance",
]


@pytest.mark.parametrize("path", READ_PATHS)
def test_reads_require_auth_when_protected(monkeypatch, path):
    client = _client(monkeypatch, "true")
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer garbage"}).status_code == 401


@pytest.mark.parametrize("path", READ_PATHS)
def test_reads_open_when_unprotected(monkeypatch, path):
    """Paper must stay reachable without a token — a 401 here would break the
    existing public dashboard. Any non-401 status means the gate stood down;
    upstream errors (502 from a missing broker) are fine, auth is what's tested."""
    assert _client(monkeypatch, "false").get(path).status_code != 401


def test_flag_defaults_to_open(monkeypatch):
    """Absent PROTECT_READS behaves as paper — deployments that never heard of
    this flag (Render, local dev) must not start 401-ing."""
    monkeypatch.delenv("PROTECT_READS", raising=False)
    deps._protect_reads.cache_clear()
    assert deps._protect_reads() is False
