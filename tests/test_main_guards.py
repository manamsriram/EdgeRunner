"""Multi-worker guard: the in-process schedulers must refuse to start under WEB_CONCURRENCY>1,
or every extra web worker double-submits every order."""
from __future__ import annotations

from unittest import mock

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


def test_migration_takes_advisory_lock_for_postgres(monkeypatch):
    """A Postgres migration must hold a session advisory lock for the whole run and
    release it (close the connection) in finally, so a timed-out/restarting second
    process blocks instead of running concurrent DDL."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)

    conn = mock.MagicMock()
    engine = mock.MagicMock()
    engine.connect.return_value = conn

    with mock.patch("sqlalchemy.create_engine", return_value=engine) as ce, \
         mock.patch("alembic.command.upgrade") as upgrade:
        main._run_migrations()

    ce.assert_called_once()
    upgrade.assert_called_once()
    # Locked with the shared key before the upgrade, released via close() in finally.
    conn.execute.assert_called_once()
    assert conn.execute.call_args.args[1] == {"k": main._MIGRATION_LOCK_KEY}
    conn.close.assert_called_once()


def test_migration_skips_lock_for_non_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp.db")
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)

    with mock.patch("sqlalchemy.create_engine") as ce, \
         mock.patch("alembic.command.upgrade") as upgrade:
        main._run_migrations()

    ce.assert_not_called()
    upgrade.assert_called_once()
