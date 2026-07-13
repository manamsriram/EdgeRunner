"""Portfolio persistence: the audit + approval-queue store.

Holds orders, trades, signals, runs, and proposals (the manual-approval queue for the
Phase 4 decision gate). It deliberately does NOT store positions — those come live from
`broker.reconcile()`, which is the source of truth.

Production uses `PostgresRepository` (Supabase). `SQLiteRepository` is kept in
`trader/portfolio/sqlite_repo.py` for test use only.
"""
from trader.portfolio.repository import (
    OrderRow,
    PortfolioRepository,
    ProposalRow,
    SignalRow,
)

__all__ = [
    "OrderRow",
    "PortfolioRepository",
    "ProposalRow",
    "SignalRow",
]
