"""Portfolio persistence: the audit + approval-queue store.

Holds orders, trades, signals, runs, and proposals (the manual-approval queue for the
Phase 4 decision gate). It deliberately does NOT store positions — those come live from
`broker.reconcile()`, which is the source of truth.

The `PortfolioRepository` interface comes first so a Supabase/Postgres adapter can drop
in later untouched; the local `SQLiteRepository` ships now so the order path is testable
and runnable with zero provisioning.
"""
from trader.portfolio.repository import (
    OrderRow,
    PortfolioRepository,
    ProposalRow,
    SignalRow,
    TradeRow,
)
from trader.portfolio.sqlite_repo import SQLiteRepository

__all__ = [
    "OrderRow",
    "PortfolioRepository",
    "ProposalRow",
    "SQLiteRepository",
    "SignalRow",
    "TradeRow",
]
