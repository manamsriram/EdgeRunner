"""The PortfolioRepository contract + row types.

Interface-first so the storage backend is swappable: SQLite now, Supabase/Postgres later
behind the exact same methods. Rows are plain dataclasses on the way in; reads return
dicts (decoupled from any backend's native row type).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

# Proposal lifecycle states (the manual-approval queue for the Phase 4 decision gate).
PROPOSAL_PENDING = "pending"
PROPOSAL_APPROVED = "approved"
PROPOSAL_REJECTED = "rejected"
PROPOSAL_EXECUTED = "executed"


@dataclass(frozen=True)
class SignalRow:
    run_id: int
    symbol: str
    side: str
    strength: float
    reason: str


@dataclass(frozen=True)
class OrderRow:
    client_order_id: str
    symbol: str
    side: str
    notional: float
    status: str
    broker_order_id: str | None = None


@dataclass(frozen=True)
class TradeRow:
    symbol: str
    side: str
    qty: float
    price: float


@dataclass(frozen=True)
class ProposalRow:
    symbol: str
    side: str
    notional: float
    ref_price: float
    reason: str


class PortfolioRepository(ABC):
    """Persistence for the order path's audit trail and approval queue."""

    @abstractmethod
    def record_run(self, strategy: str, mode: str, note: str = "") -> int:
        """Open a pipeline run; returns its id (used to tie signals to the run)."""

    @abstractmethod
    def record_signal(self, signal: SignalRow) -> int: ...

    @abstractmethod
    def record_order(self, order: OrderRow) -> int:
        """Persist an order. Idempotent on `client_order_id`: a repeat returns the
        existing row's id and does not insert a duplicate."""

    @abstractmethod
    def record_trade(self, trade: TradeRow) -> int: ...

    @abstractmethod
    def create_proposal(self, proposal: ProposalRow) -> int: ...

    @abstractmethod
    def list_pending_proposals(self) -> list[dict]: ...

    @abstractmethod
    def set_proposal_status(self, proposal_id: int, status: str) -> None: ...

    @abstractmethod
    def get_orders(self) -> list[dict]: ...

    @abstractmethod
    def get_runs(self) -> list[dict]: ...

    @abstractmethod
    def get_strategy_signal_counts(self) -> dict[str, int]:
        """Return signal count per strategy for all auto-mode runs.
        Keys are strategy class names (e.g. "MomentumRSI"); values are signal counts.
        Returns {} if no data."""
