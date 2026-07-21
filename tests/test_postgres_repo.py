"""Tests for PostgresRepository schema migrations and helpers.

Uses fake cursors so no real Postgres is required.
"""
from __future__ import annotations

import pytest

from trader.portfolio.postgres_repo import PostgresRepository


class _FakeCursor:
    """Records executed statements and returns canned rows for the PK check."""

    def __init__(self, pk_columns: list[str] | None = None) -> None:
        self.statements: list[str] = []
        self._pk_columns = pk_columns if pk_columns is not None else ["symbol"]

    def execute(self, sql: str, params=None) -> None:  # noqa: ANN001 - params are variadic
        self.statements.append(sql)

    def fetchall(self) -> list[dict]:
        # The only SELECT we expect is the PK column check.
        return [{"attname": col} for col in self._pk_columns]


@pytest.fixture
def old_pk_cursor() -> _FakeCursor:
    return _FakeCursor(pk_columns=["symbol"])


@pytest.fixture
def new_pk_cursor() -> _FakeCursor:
    return _FakeCursor(pk_columns=["symbol", "pool"])


def test_migration_runs_for_old_single_column_pk(old_pk_cursor: _FakeCursor) -> None:
    PostgresRepository._migrate_position_owners_pool_pk(old_pk_cursor)

    executed = "\n".join(old_pk_cursor.statements)
    # Should backfill NULL pools.
    assert "UPDATE position_owners SET pool = 'daily' WHERE pool IS NULL" in executed
    # Should deduplicate before adding the new PK.
    assert "DELETE FROM position_owners" in executed
    assert "DISTINCT ON (symbol, pool)" in executed
    # Should drop the old PK and add the composite one.
    assert "ALTER TABLE position_owners DROP CONSTRAINT position_owners_pkey" in executed
    assert "ALTER TABLE position_owners ADD PRIMARY KEY (symbol, pool)" in executed


def test_migration_skips_when_composite_pk_already_exists(new_pk_cursor: _FakeCursor) -> None:
    PostgresRepository._migrate_position_owners_pool_pk(new_pk_cursor)

    # The only query should be the initial PK introspection; no ALTER/DROP/DELETE.
    assert len(new_pk_cursor.statements) == 1
    assert "ALTER TABLE position_owners" not in new_pk_cursor.statements[0]


def test_migration_skips_when_no_pk_exists() -> None:
    cursor = _FakeCursor(pk_columns=[])
    PostgresRepository._migrate_position_owners_pool_pk(cursor)
    # Brand-new table (no PK yet) should not be touched.
    assert len(cursor.statements) == 1
    assert "ALTER TABLE" not in cursor.statements[0]
