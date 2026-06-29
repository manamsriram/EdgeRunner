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
    strategy_name: str | None = None
    regime: str | None = None
    signal_strength: float | None = None


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
    def try_approve_proposal(self, proposal_id: int) -> dict | None:
        """Atomically claim a pending proposal for approval.

        Does UPDATE WHERE status='pending'. Returns proposal dict on success, None if
        already claimed or resolved by a concurrent request (caller should return 409).
        """

    @abstractmethod
    def get_orders(self) -> list[dict]: ...

    @abstractmethod
    def get_runs(self) -> list[dict]: ...

    @abstractmethod
    def get_strategy_signal_counts(self) -> dict[str, int]:
        """Return signal count per strategy for all auto-mode runs.
        Keys are strategy class names (e.g. "MomentumRSI"); values are signal counts.
        Returns {} if no data."""

    @abstractmethod
    def get_position_owners(self) -> dict[tuple[str, str], str]:
        """Return all persisted ownership entries: (symbol, pool) -> strategy class name."""

    @abstractmethod
    def set_position_owner(self, symbol: str, strategy: str, pool: str = "daily") -> None:
        """Upsert ownership of (symbol, pool) to strategy. Called when a buy executes."""

    @abstractmethod
    def clear_position_owner(self, symbol: str, pool: str = "daily") -> None:
        """Remove ownership for (symbol, pool). Called when a sell executes."""

    @abstractmethod
    def get_bandit_weight(self, strategy: str, regime: str) -> float:
        """Return stored weight for (strategy, regime); 1.0 if never set."""

    @abstractmethod
    def save_bandit_weight(self, strategy: str, regime: str, weight: float, cycle_index: int) -> None:
        """Upsert (strategy, regime) weight from the nightly batch."""

    @abstractmethod
    def get_all_bandit_weights(self) -> dict[tuple[str, str], tuple[float, int]]:
        """Return all stored weights as {(strategy, regime): (weight, cycle_index)}."""

    @abstractmethod
    def save_bandit_arm(self, strategy: str, regime: str,
                        alpha: int, beta: int, cycle_index: int, weight: float) -> None:
        """Upsert (strategy, regime) Thompson arm counts and sampled weight."""

    @abstractmethod
    def get_all_bandit_arms(self) -> dict[tuple[str, str], tuple[int, int, int]]:
        """Return {(strategy, regime): (alpha_wins, beta_losses, cycle_index)}."""

    @abstractmethod
    def append_ic_observation(self, strategy: str, regime: str, ic: float, ts: str) -> None:
        """Append one IC data point to arm_ic_series."""

    @abstractmethod
    def get_ic_series(self, strategy: str, regime: str, limit: int = 60) -> list[float]:
        """Return the most recent `limit` IC values, oldest-first."""
