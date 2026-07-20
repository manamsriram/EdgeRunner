"""Nightly linked-data count check for the ML-overlay research track.

Reports how many decision_features rows have produced a real order (order_id
set) — this is the Phase 1 minimum-data gate the plan requires before Phase 2
training starts (~500 linked rows, ~30 losing-trade outcomes among them). The
losing-trade count needs an orders.id <-> trade_outcomes join that doesn't
exist yet (record_trade_outcome doesn't attach an order id today) — that join
is Phase 2 scope, tracked in the plan, not silently done here.
"""
from __future__ import annotations

from trader.portfolio.repository import PortfolioRepository


def count_linked_decision_features(repo: PortfolioRepository) -> int:
    """Count decision_features rows that produced an order (order_id set)."""
    return repo.get_decision_features_count(linked_only=True)
