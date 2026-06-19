import pytest
from trader.portfolio.sqlite_repo import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(str(tmp_path / "test.db"))


def test_append_and_get_ic_series(repo):
    repo.append_ic_observation("DonchianBreakout", "calm", 0.15, "2026-01-01T00:00:00")
    repo.append_ic_observation("DonchianBreakout", "calm", 0.20, "2026-01-02T00:00:00")
    series = repo.get_ic_series("DonchianBreakout", "calm")
    assert series == pytest.approx([0.15, 0.20])


def test_get_ic_series_returns_oldest_first(repo):
    repo.append_ic_observation("SuperTrend", "normal", 0.30, "2026-01-03T00:00:00")
    repo.append_ic_observation("SuperTrend", "normal", 0.10, "2026-01-01T00:00:00")
    repo.append_ic_observation("SuperTrend", "normal", 0.20, "2026-01-02T00:00:00")
    series = repo.get_ic_series("SuperTrend", "normal")
    assert series[0] < series[-1]  # oldest IC came first (0.10 < 0.30)


def test_get_ic_series_empty_returns_empty(repo):
    assert repo.get_ic_series("NoArm", "none") == []


def test_get_ic_series_respects_limit(repo):
    for i in range(10):
        repo.append_ic_observation("S", "r", float(i) * 0.01, f"2026-01-{i+1:02d}T00:00:00")
    series = repo.get_ic_series("S", "r", limit=3)
    assert len(series) == 3


def test_record_order_stores_signal_strength(repo):
    from trader.portfolio.repository import OrderRow
    order = OrderRow(
        client_order_id="test-123",
        symbol="AAPL",
        side="buy",
        notional=1000.0,
        status="filled",
        strategy_name="DonchianBreakout",
        regime="calm",
        signal_strength=0.75,
    )
    repo.record_order(order)
    orders = repo.get_orders()
    assert orders[0]["signal_strength"] == pytest.approx(0.75)


def test_record_order_signal_strength_nullable(repo):
    from trader.portfolio.repository import OrderRow
    order = OrderRow(
        client_order_id="test-456",
        symbol="AAPL",
        side="buy",
        notional=1000.0,
        status="filled",
    )
    repo.record_order(order)
    orders = repo.get_orders()
    assert orders[0]["signal_strength"] is None
