import pytest

from trader.learning.link_outcomes import count_linked_decision_features
from trader.portfolio.repository import DecisionFeaturesRow
from trader.portfolio.sqlite_repo import SQLiteRepository


@pytest.fixture
def repo(tmp_path) -> SQLiteRepository:
    return SQLiteRepository(str(tmp_path / "portfolio.db"))


def test_count_linked_decision_features(repo):
    run_id = repo.record_run(strategy="DipRecovery", mode="auto")
    repo.record_decision_features(DecisionFeaturesRow(
        run_id=run_id, symbol="AAPL", side="buy", strategy="DipRecovery",
        regime="normal", signal_strength_pre_overlay=0.8, features={},
    ))
    assert count_linked_decision_features(repo) == 0  # no order_id set yet

    repo.link_order_to_decision_features(run_id=run_id, order_id=1)
    assert count_linked_decision_features(repo) == 1
