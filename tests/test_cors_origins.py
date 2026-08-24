"""FRONTEND_ORIGIN accepts a comma-separated list.

A single-origin value silently 400s every preflight from the other URL Vercel
serves the same build on, which reaches the user as a broken dashboard tab
rather than as a CORS error.
"""
import importlib

from fastapi.testclient import TestClient


def _client(frontend_origin, monkeypatch):
    monkeypatch.setenv("FRONTEND_ORIGIN", frontend_origin)
    import api.main

    return TestClient(importlib.reload(api.main).app)


def _preflight(client, origin):
    return client.options(
        "/api/controls/logs",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )


def test_each_listed_origin_is_allowed(monkeypatch):
    client = _client("https://alias.vercel.app, https://deployment.vercel.app", monkeypatch)
    for origin in ("https://alias.vercel.app", "https://deployment.vercel.app"):
        resp = _preflight(client, origin)
        assert resp.status_code == 200, origin
        assert resp.headers["access-control-allow-origin"] == origin


def test_unlisted_origin_is_rejected(monkeypatch):
    client = _client("https://alias.vercel.app", monkeypatch)
    assert _preflight(client, "https://evil.example.com").status_code == 400
