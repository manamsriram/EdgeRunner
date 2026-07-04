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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trader.config import RiskLimits

Side = str  # "buy" | "sell"

# A buy sized to within this many dollars of the position cap is treated as a no-op
# rather than a fill — avoids submitting dust orders that just churn commission.
_NO_OP_EPSILON = 10.0


def is_crypto_symbol(symbol: str) -> bool:
    """Return True for crypto pairs (contain '/': BTC/USD, ETH/USDT, etc.)."""
    return "/" in symbol


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
    cash: float = 0.0                      # uninvested cash available for new buys (defaults to 0 for compat)
    avg_entry_prices: dict[str, float] = field(default_factory=dict)  # symbol -> avg cost basis
    position_owners: dict[str, str] = field(default_factory=dict)     # symbol -> owning strategy class name
    deployed_notional: float = 0.0         # cumulative buy notional approved this tick (daily pool)
    intraday_deployed: float = 0.0         # cumulative buy notional approved this tick (intraday pool)
    last_losing_exit_at: dict[str, datetime] = field(default_factory=dict)  # symbol -> most recent losing exit ts


@dataclass(frozen=True)
class OrderIntent:
    """A proposed order, before the gate. `notional` is a dollar amount; `ref_price` is
    the latest price for the symbol, used to project the resulting position value."""

    symbol: str
    side: Side
    notional: float
    ref_price: float
    reason: str = ""
    spread_pct: float = 0.0  # bid-ask spread as fraction of mid; 0 = unknown
    pool: str = "daily"                    # "daily" or "intraday" — determines capital pool cap

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
        now: datetime | None = None,
    ) -> RiskDecision:
        limits = self._limits
        now = now or datetime.now(timezone.utc)

        # 0. Kill switch / unknown state — fail closed before anything else.
        if kill_switch is not None and kill_switch.engaged():
            return RiskDecision.reject("kill switch engaged")
        if state.stale:
            return RiskDecision.reject("account state stale (reconciliation failed)")

        # 0b. Transaction cost check (buys only) — skip if spread exceeds threshold.
        #     Round-trip cost ≈ 2 × spread_pct. Only fires when spread data is available.
        if intent.side == "buy" and intent.spread_pct > 0:
            round_trip_cost = 2.0 * intent.spread_pct
            if round_trip_cost > limits.max_spread_pct:
                return RiskDecision.reject(
                    f"spread too wide: round-trip cost {round_trip_cost:.3%} "
                    f"> max {limits.max_spread_pct:.3%}"
                )

        # 1. Allowlist — route by asset type.
        # None means dynamic/open universe; skip the check entirely.
        _is_crypto = is_crypto_symbol(intent.symbol)
        _active_allowlist = limits.crypto_allowlist if _is_crypto else limits.allowlist
        if _active_allowlist is not None and intent.symbol not in _active_allowlist:
            return RiskDecision.reject(f"{intent.symbol} not in allowlist")

        # 2. Pending order — positions don't reflect an in-flight order; refuse to stack.
        if intent.symbol in state.open_order_symbols:
            return RiskDecision.reject(f"{intent.symbol} has an unfilled order in flight")

        # 2b. Symbol cooldown — block new entries shortly after a losing exit on this
        #     symbol, to prevent immediate revenge re-entry chasing a setup that just
        #     failed. Buys only; a cooldown must never block closing a position.
        if intent.side == "buy" and limits.symbol_cooldown_enabled:
            last_loss = state.last_losing_exit_at.get(intent.symbol)
            if last_loss is not None:
                elapsed = (now - last_loss).total_seconds()
                if elapsed < limits.symbol_cooldown_seconds:
                    return RiskDecision.reject(
                        f"{intent.symbol} in cooldown: last losing exit "
                        f"{elapsed / 60:.0f}m ago, cooldown={limits.symbol_cooldown_seconds / 60:.0f}m"
                    )

        # 3. Daily-loss halt is enforced in the buy path only (after the sell branch),
        #    so a daily loss never traps you in a crashing position — sells stay open.

        # 4b. PDT guard — US equity FINRA rule only; does not apply to crypto.
        #     trades_today counts individual fills; a day-trade is a buy+sell pair, so
        #     trades_today // 2 gives completed round-trips. Blocking at >= limit prevents
        #     the 4th round-trip entry. Sells always pass — closing positions is never blocked.
        if (
            not _is_crypto
            and intent.side == "buy"
            and state.equity < limits.pdt_equity_threshold
            and state.trades_today // 2 >= limits.pdt_day_trade_limit
        ):
            return RiskDecision.reject(
                f"PDT guard: {state.trades_today // 2} day-trades today "
                f"(limit {limits.pdt_day_trade_limit}) on equity "
                f"${state.equity:,.0f} < ${limits.pdt_equity_threshold:,.0f}"
            )

        # 5. Side sanity — long/flat only, no shorting. A sell may never exceed the held
        #    value, so even a notional misuse downstream cannot open a short.
        held = state.positions.get(intent.symbol, 0.0)
        if intent.side == "sell":
            if held <= 0.0:
                return RiskDecision.reject(f"no {intent.symbol} position to sell")
            held_value = held * intent.ref_price
            approved = min(intent.notional, held_value)
            if approved <= _NO_OP_EPSILON:
                return RiskDecision.reject("approved notional below minimum")
            return RiskDecision.approve(approved, "sell approved")

        # 5b. Daily-loss halt (buys only, opt-in). Blocks NEW buys once the day's drawdown
        #     hits the limit; sells already returned above, so exits are never blocked.
        #     Skips when daily_pnl_pct is None (e.g. CCXT has no last_equity) — never a hard
        #     reject, so an unknown P&L cannot freeze the whole book.
        if (
            limits.daily_loss_halt_enabled
            and limits.require_daily_pnl_check
            and state.daily_pnl_pct is not None
            and state.daily_pnl_pct <= -limits.daily_loss_limit_pct
        ):
            return RiskDecision.reject(
                f"daily loss {state.daily_pnl_pct:.2%} hit limit "
                f"-{limits.daily_loss_limit_pct:.2%} — new buys halted"
            )

        # 6. Max position size (buys only) — cap is a fraction of pool equity, not total equity.
        _cap_pct = limits.max_crypto_position_pct if _is_crypto else limits.max_position_pct
        _pool_fraction = (
            limits.intraday_pool_pct
            if intent.pool == "intraday"
            else (1.0 - limits.intraday_pool_pct)
        )
        cap = _cap_pct * state.equity * _pool_fraction
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
