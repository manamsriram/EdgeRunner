"""The risk gate — hard guardrails on the order path.

Every order, whether a human approved it or autonomy emitted it, passes the *same*
`RiskGate.evaluate`. The gate is a pure function of (intent, account state, kill switch)
so the checks are trivially unit-testable with no broker or network.

THE FAIL-CLOSED RULE: if we cannot prove an order is within limits, we reject it. A
failed reconciliation (`stale`), an unknown daily P&L (`None`), or an engaged kill
switch all halt trading. Trading blind is worse than not trading.

Checks run in a fixed order and the first failure wins, so a rejection reason is always
the single most important thing wrong.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from trader.config import RiskLimits

Side = str  # "buy" | "sell"

# A buy sized to within this many dollars of the position cap is treated as a no-op
# rather than a fill — avoids submitting dust orders that just churn commission.
_NO_OP_EPSILON = 1.0


@dataclass(frozen=True)
class AccountState:
    """Everything the gate needs about the account, sourced from broker reconciliation.

    `stale` is set when reconciliation failed or was partial; the gate rejects on it so
    we never act on an unknown position/equity state (prevents double-buys after an API
    hiccup). `daily_pnl_pct` is None when it could not be computed — also a rejection.
    """

    equity: float
    positions: dict[str, float]            # symbol -> shares currently held
    open_order_symbols: frozenset[str]     # symbols with an unfilled order in flight
    trades_today: int
    daily_pnl_pct: float | None
    stale: bool = False


@dataclass(frozen=True)
class OrderIntent:
    """A proposed order, before the gate. `notional` is a dollar amount; `ref_price` is
    the latest price for the symbol, used to project the resulting position value."""

    symbol: str
    side: Side
    notional: float
    ref_price: float
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        if self.side not in {"buy", "sell"}:
            raise ValueError(f"invalid side: {self.side!r}")
        if self.notional <= 0:
            raise ValueError(f"notional must be > 0, got {self.notional}")
        if self.ref_price <= 0:
            raise ValueError(f"ref_price must be > 0, got {self.ref_price}")


@dataclass(frozen=True)
class RiskDecision:
    """The gate's verdict. `approved_notional` is 0.0 when rejected, and may be smaller
    than the intent's notional when a buy was sized down to the position cap."""

    approved: bool
    reason: str
    approved_notional: float = 0.0

    @classmethod
    def reject(cls, reason: str) -> RiskDecision:
        return cls(approved=False, reason=reason, approved_notional=0.0)

    @classmethod
    def approve(cls, notional: float, reason: str = "ok") -> RiskDecision:
        return cls(approved=True, reason=reason, approved_notional=notional)


class KillSwitch:
    """A file-backed halt. Presence of the file means "stop trading", so it survives a
    process crash and can be tripped out-of-band (touch the file) independent of any UI.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def engaged(self) -> bool:
        return self._path.exists()

    def engage(self, note: str = "") -> None:
        # Write-then-replace so a reader never sees a half-written flag.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(note or "engaged")
        os.replace(tmp, self._path)

    def disengage(self) -> None:
        self._path.unlink(missing_ok=True)


class RiskGate:
    """Stateless evaluator built from `RiskLimits`. The same instance serves the manual
    and autonomous paths — that sameness is what makes flipping AUTONOMY safe later."""

    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def evaluate(
        self,
        intent: OrderIntent,
        state: AccountState,
        kill_switch: KillSwitch | None = None,
    ) -> RiskDecision:
        limits = self._limits

        # 0. Kill switch / unknown state — fail closed before anything else.
        if kill_switch is not None and kill_switch.engaged():
            return RiskDecision.reject("kill switch engaged")
        if state.stale:
            return RiskDecision.reject("account state stale (reconciliation failed)")

        # 1. Allowlist.
        if intent.symbol not in limits.allowlist:
            return RiskDecision.reject(f"{intent.symbol} not in allowlist")

        # 2. Pending order — positions don't reflect an in-flight order; refuse to stack.
        if intent.symbol in state.open_order_symbols:
            return RiskDecision.reject(f"{intent.symbol} has an unfilled order in flight")

        # 3. Daily-loss circuit breaker (None = unprovable = reject).
        if state.daily_pnl_pct is None:
            return RiskDecision.reject("daily P&L unknown")
        if state.daily_pnl_pct <= -limits.daily_loss_limit_pct:
            return RiskDecision.reject(
                f"daily loss {state.daily_pnl_pct:.2%} hit limit "
                f"-{limits.daily_loss_limit_pct:.2%}"
            )

        # 4. Churn circuit breaker.
        if state.trades_today >= limits.max_trades_per_day:
            return RiskDecision.reject(
                f"max trades/day reached ({state.trades_today}/{limits.max_trades_per_day})"
            )

        # 5. Side sanity — long/flat only, no shorting. A sell may never exceed the held
        #    value, so even a notional misuse downstream cannot open a short.
        held = state.positions.get(intent.symbol, 0.0)
        if intent.side == "sell":
            if held <= 0.0:
                return RiskDecision.reject(f"no {intent.symbol} position to sell")
            held_value = held * intent.ref_price
            return RiskDecision.approve(
                min(intent.notional, held_value), "sell approved"
            )

        # 6. Max position size (buys only) — size down to the cap, or reject as a no-op.
        cap = limits.max_position_pct * state.equity
        existing_value = held * intent.ref_price
        headroom = cap - existing_value
        if headroom <= _NO_OP_EPSILON:
            return RiskDecision.reject(
                f"{intent.symbol} already at position cap "
                f"(${existing_value:,.0f} of ${cap:,.0f})"
            )
        approved = min(intent.notional, headroom)
        if approved <= _NO_OP_EPSILON:
            return RiskDecision.reject("approved notional below minimum")
        if approved < intent.notional:
            return RiskDecision.approve(
                approved, f"sized down to position cap (${approved:,.0f})"
            )
        return RiskDecision.approve(approved, "buy approved")
