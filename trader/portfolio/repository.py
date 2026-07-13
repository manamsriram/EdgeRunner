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
    entry_rationale: str | None = None  # buy-side only: overlay's approval rationale


@dataclass(frozen=True)
class TradeOutcomeRow:
    """A closed round-trip trade, written once a sell fills. Feeds the symbol
    cooldown guard (RiskGate) and the overlay's trade-memory prompt context."""

    symbol: str
    strategy: str
    regime: str
    side: str                              # entry side; always "buy" today (long/flat-only)
    entry_price: float
    exit_price: float
    pnl_pct: float
    exit_reason: str                       # "stop-loss" | "signal-exit" | "eod-exit"
    entry_overlay_rationale: str | None
    closed_at: str                         # ISO-8601 UTC


@dataclass(frozen=True)
class OptionsPositionRow:
    """One options contract position — CSP or CC, opened by CSP-on-dip or the Wheel.

    `wheel_state` is one of "csp_open" | "assigned" | "cc_open" | "called_away" | "csp_expired"
    | "cc_expired"; CSP-on-dip positions that aren't part of a Wheel cycle use "csp_open" /
    "csp_expired" only. `collateral` is cash reserved for a CSP (100 * strike) or the market
    value of shares reserved for a CC — whichever this contract ties up.
    """

    contract_symbol: str        # OCC symbol, e.g. AAPL260116P00150000
    underlying: str
    option_type: str            # "call" | "put" (not "right" — reserved word in Postgres)
    strike: float
    expiry: str                 # ISO date
    opening_order_id: str       # client_order_id of the order that opened this contract
    strategy: str                # "csp_on_dip" | "wheel"
    collateral: float
    wheel_state: str = "csp_open"
    status: str = "open"         # "open" | "closed"

    def __post_init__(self) -> None:
        if self.option_type not in {"call", "put"}:
            raise ValueError(f"invalid option_type: {self.option_type!r}")


# wheel_state values that terminate a position (must pair with status="closed").
TERMINAL_WHEEL_STATES = {"called_away", "csp_expired", "cc_expired"}


def expected_options_status(wheel_state: str) -> str:
    """The status a wheel_state implies — "closed" for terminal states, else "open"."""
    return "closed" if wheel_state in TERMINAL_WHEEL_STATES else "open"


def validate_options_transition(wheel_state: str | None, status: str | None) -> None:
    """Raise if a wheel_state/status pair being written would desync the two fields."""
    if wheel_state is not None and status is not None:
        if status != expected_options_status(wheel_state):
            raise ValueError(
                f"wheel_state={wheel_state!r} requires status={expected_options_status(wheel_state)!r}, got {status!r}"
            )


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
    def get_last_buy_order(self, symbol: str) -> dict | None:
        """Most recent side='buy' order for symbol, or None. Indexed lookup —
        do not use get_orders() + client-side filtering for this."""

    @abstractmethod
    def get_orders_by_status(self, status: str, since_ts: str) -> list[dict]:
        """Orders with the given status whose ts >= since_ts (ISO-8601), oldest-first.
        Used by order-status reconciliation to find rows stuck at 'submitted'."""

    @abstractmethod
    def record_trade_outcome(self, outcome: TradeOutcomeRow) -> int: ...

    @abstractmethod
    def get_recent_outcomes(
        self,
        symbol: str | None = None,
        strategy: str | None = None,
        regime: str | None = None,
        limit: int = 3,
    ) -> list[dict]:
        """Most-recent-first closed trades matching the given filters. Any of
        symbol/strategy/regime left None widens the match (no filter on that column)."""

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

    @abstractmethod
    def get_overlay_cache(self, symbol: str, side: str, ttl_seconds: float) -> dict | None:
        """Return the cached overlay result for (symbol, side) if younger than
        ttl_seconds, else None. Keys: side, strength, reason (of the cached result,
        not the request). DB-backed so the cache survives process restarts/redeploys."""

    @abstractmethod
    def set_overlay_cache(self, symbol: str, side: str, result_side: str,
                          result_strength: float, result_reason: str) -> None:
        """Upsert the overlay result cached for (symbol, side)."""

    @abstractmethod
    def record_options_position(self, position: OptionsPositionRow) -> int:
        """Persist a newly opened options contract. Idempotent on `opening_order_id` —
        a retried record for the same order returns the existing row's id rather than
        inserting a duplicate."""

    @abstractmethod
    def update_options_position(
        self, contract_symbol: str, *, wheel_state: str | None = None, status: str | None = None,
        collateral: float | None = None,
    ) -> None:
        """Update wheel_state/status/collateral for the *open* contract matching
        `contract_symbol`. Leaves fields None-d as-is. Only matches rows with
        status='open' — contract_symbol is not unique across wheel cycles, so a stale
        closed row that happens to share a symbol is never touched.

        Pass `collateral=0.0` when transitioning a CSP to "assigned": the cash it
        reserved has now been spent buying the underlying shares, so counting it still
        as options collateral would double-count that capital against the stock
        position `RiskGate` already sees via `AccountState.positions`.
        """

    @abstractmethod
    def get_open_options_positions(self, underlying: str | None = None) -> list[dict]:
        """Open contracts, optionally filtered to one underlying. Used for collateral
        sums (RiskGate) and Wheel state-machine resumption."""

    def get_total_options_collateral(self) -> float:
        """Sum of `collateral` across all open options positions.

        The single source of truth for options exposure — call sites should use this
        instead of summing `get_open_options_positions()` themselves, so a future call
        site can't drift out of sync with the others.
        """
        return sum(p["collateral"] for p in self.get_open_options_positions())

    @abstractmethod
    def record_llm_call(
        self,
        provider: str,
        call_site: str,
        symbol: str,
        cache_hit: bool,
        input_tokens: int,
        output_tokens: int,
        est_cost_usd: float,
    ) -> None:
        """Append one row to the LLM call log (Phase 0 cost measurement).

        `call_site` is "overlay" or "fundamental_gate". Cache hits are logged too
        (cache_hit=True, tokens=0, est_cost_usd=0.0) so hit-rate is visible
        alongside raw call volume."""
