"""PostgresRepository options-position behavior parity with SQLiteRepository.

Skipped unless DATABASE_URL_TEST points at a real Postgres (CI provides one via a
service container — see .github/workflows/ci.yml). Locally: run a throwaway Postgres
and export DATABASE_URL_TEST, e.g.
    docker run --rm -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:16
    export DATABASE_URL_TEST=postgresql://postgres:test@localhost:5432/postgres
"""
from __future__ import annotations

import os

import pytest

from trader.portfolio.repository import OptionsPositionRow

DATABASE_URL_TEST = os.environ.get("DATABASE_URL_TEST")

pytestmark = pytest.mark.skipif(
    DATABASE_URL_TEST is None, reason="no test Postgres available (set DATABASE_URL_TEST)"
)


@pytest.fixture
def postgres_repo():
    from trader.portfolio.postgres_repo import PostgresRepository

    repo = PostgresRepository(DATABASE_URL_TEST)
    yield repo
    with repo._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE options_positions RESTART IDENTITY")


def test_options_position_crud(postgres_repo):
    row = OptionsPositionRow(
        contract_symbol="AAPL260116P00150000", underlying="AAPL", option_type="put",
        strike=150.0, expiry="2026-01-16", opening_order_id="abc123",
        strategy="csp_on_dip", collateral=15_000.0,
    )
    postgres_repo.record_options_position(row)
    open_positions = postgres_repo.get_open_options_positions("AAPL")
    assert len(open_positions) == 1
    assert open_positions[0]["wheel_state"] == "csp_open"

    postgres_repo.update_options_position("AAPL260116P00150000", wheel_state="assigned")
    open_positions = postgres_repo.get_open_options_positions("AAPL")
    assert open_positions[0]["wheel_state"] == "assigned"

    postgres_repo.update_options_position("AAPL260116P00150000", status="closed")
    assert postgres_repo.get_open_options_positions("AAPL") == []


def test_options_position_insert_idempotent_on_opening_order_id(postgres_repo):
    row = OptionsPositionRow(
        contract_symbol="AAPL260116P00150000", underlying="AAPL", option_type="put",
        strike=150.0, expiry="2026-01-16", opening_order_id="abc123",
        strategy="csp_on_dip", collateral=15_000.0,
    )
    id1 = postgres_repo.record_options_position(row)
    id2 = postgres_repo.record_options_position(row)
    assert id1 == id2
    assert len(postgres_repo.get_open_options_positions("AAPL")) == 1


def test_options_position_same_contract_symbol_reopens_across_wheel_cycles(postgres_repo):
    first = OptionsPositionRow(
        contract_symbol="AAPL260116P00150000", underlying="AAPL", option_type="put",
        strike=150.0, expiry="2026-01-16", opening_order_id="oid_cycle1",
        strategy="wheel", collateral=15_000.0,
    )
    id1 = postgres_repo.record_options_position(first)
    postgres_repo.update_options_position("AAPL260116P00150000", wheel_state="csp_expired", status="closed")

    second = OptionsPositionRow(
        contract_symbol="AAPL260116P00150000", underlying="AAPL", option_type="put",
        strike=150.0, expiry="2026-01-16", opening_order_id="oid_cycle2",
        strategy="wheel", collateral=15_000.0,
    )
    id2 = postgres_repo.record_options_position(second)
    assert id2 != id1
    open_positions = postgres_repo.get_open_options_positions("AAPL")
    assert len(open_positions) == 1
    assert open_positions[0]["opening_order_id"] == "oid_cycle2"


def test_total_options_collateral_sums_open_positions(postgres_repo):
    postgres_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_P", underlying="AAPL", option_type="put", strike=140.0,
        expiry="2026-01-16", opening_order_id="oid1", strategy="csp_on_dip", collateral=14_000.0,
    ))
    postgres_repo.record_options_position(OptionsPositionRow(
        contract_symbol="MSFT_P", underlying="MSFT", option_type="put", strike=300.0,
        expiry="2026-01-16", opening_order_id="oid2", strategy="csp_on_dip", collateral=30_000.0,
    ))
    assert postgres_repo.get_total_options_collateral() == 44_000.0
    postgres_repo.update_options_position("AAPL_P", wheel_state="csp_expired", status="closed")
    assert postgres_repo.get_total_options_collateral() == 30_000.0


def test_update_options_position_rejects_inconsistent_wheel_state_and_status(postgres_repo):
    postgres_repo.record_options_position(OptionsPositionRow(
        contract_symbol="AAPL_P", underlying="AAPL", option_type="put", strike=140.0,
        expiry="2026-01-16", opening_order_id="oid1", strategy="csp_on_dip", collateral=14_000.0,
    ))
    with pytest.raises(ValueError):
        postgres_repo.update_options_position("AAPL_P", wheel_state="csp_expired", status="open")
    with pytest.raises(ValueError):
        postgres_repo.update_options_position("AAPL_P", wheel_state="assigned", status="closed")


def test_check_constraint_rejects_invalid_wheel_state(postgres_repo):
    """DB-level backstop (migration 006) in case app-level validation is ever bypassed."""
    with pytest.raises(Exception):
        with postgres_repo._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO options_positions "
                    "(contract_symbol, underlying, option_type, strike, expiry, opening_order_id, "
                    "strategy, collateral, wheel_state, status, opened_at) "
                    "VALUES ('BAD', 'AAPL', 'put', 140.0, '2026-01-16', 'oid', 'csp_on_dip', "
                    "14000.0, 'not_a_real_state', 'open', now()::text)"
                )
