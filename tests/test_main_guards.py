"""Multi-worker guard: the in-process schedulers must refuse to start under WEB_CONCURRENCY>1,
or every extra web worker double-submits every order."""
from __future__ import annotations

from api import main


def test_single_worker_default(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert main._multi_worker() is False


def test_web_concurrency_one_is_single(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    assert main._multi_worker() is False


def test_web_concurrency_multi(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    assert main._multi_worker() is True
