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

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trader.config import RiskLimits

logger = logging.getLogger(__name__)

Side = str  # "buy" | "sell"

# A buy sized to within this many dollars of the position cap is treated as a no-op
# rather than a fill — avoids submitting dust orders that just churn commission.
# Applies only to BUYs; sells must never be blocked by size minimums because a
# position that has dropped below $10 in value would otherwise be trapped forever.
_NO_OP_EPSILON = 10.0


def is_crypto_symbol(symbol: str) -> bool:
    """Return True for crypto pairs (contain '/': BTC/USD, ETH/USDT, etc.)."""
    return "/" in symbol


def is_option_symbol(symbol: str) -> bool:
    """Return True for OCC option symbols (e.g. AAPL260116P00150000).

    OCC format ends with YYMMDD + [C|P] + 8-digit strike; plain equity/crypto
    tickers never match. Used to keep options orders out of the equity
    reconciliation path (a CSP sell-to-open is an entry, not an equity exit)."""
    return bool(re.search(r"\d{6}[CP]\d{8}$", symbol))


# Daily leveraged/inverse ETFs and ETNs. These decay and reverse-split constantly
# (routinely 1-for-4 to 1-for-20) — a split multiplies the print by the ratio
# without adjusting a resting avg-entry cost basis, so a real (or flat/losing)
# position can read as a multi-hundred-percent "gain" purely from the split. An
# exact-match blocklist, not a suffix/prefix heuristic — bull/bear ticker
# conventions collide with ordinary tickers (e.g. "AAPL" ends in "L").
_LEVERAGED_ETF_SYMBOLS: frozenset[str] = frozenset({
    # Broad index 3x
    "TQQQ", "SQQQ", "SPXL", "SPXS", "SPXU", "UPRO", "TNA", "TZA", "URTY", "SRTY",
    # Sector 3x (Direxion)
    "SOXL", "SOXS", "LABU", "LABD", "NUGT", "DUST", "JNUG", "JDST", "FAS", "FAZ",
    "YINN", "YANG", "ERX", "ERY", "GUSH", "DRIP", "NAIL", "WEBL", "WEBS", "TPOR",
    "RETL", "PILL", "MEXX", "INDL", "CURE", "TECL", "TECS", "DFEN", "DPST", "UTSL",
    # Rates / bonds 3x
    "TMF", "TMV", "TYD", "TYO",
    # Volatility
    "UVXY", "SVXY", "VXX", "VIXY", "UVIX", "SVIX",
    # Single-stock leveraged (2026 crop — proliferates fast, kept best-effort)
    "TSLL", "TSLQ", "TSLZ", "NVDL", "NVDD", "NVDU", "AMDL", "AMDD", "AMDU",
    "MSFU", "MSFD", "AAPU", "AAPD", "GGLL", "GGLS", "METU", "METD", "MSTU", "MSTX",
    "CONL", "COIU", "COIN2X", "SMCX", "PLTU",
    # Recently observed in this book (2026-07): decay-prone single-stock/theme
    # leveraged products that don't fit a clean naming pattern.
    "BMNU", "RKLZ",
})


def is_leveraged_etf_symbol(symbol: str) -> bool:
    """Return True for known daily-leveraged/inverse ETFs, ETNs, and single-stock
    leveraged products — see `_LEVERAGED_ETF_SYMBOLS` for why these get a hard block."""
    return symbol in _LEVERAGED_ETF_SYMBOLS


# Fund-name keywords issuers use for daily-leveraged/inverse products (Direxion,
# ProShares, GraniteShares, etc. all use these consistently in the *name*, e.g.
# "Direxion Daily Semiconductor Bull 3X Shares" or "ProShares UltraPro QQQ").
# Ordinary equity/company names don't collide with these the way tickers do —
# catches new launches the exact-match symbol list hasn't caught up to yet.
_LEVERAGED_ETF_NAME_KEYWORDS: tuple[str, ...] = (
    "2x", "3x", "ultrapro", "ultrashort", "ultra ", "daily bull", "daily bear",
    "leveraged", "inverse",
)


def is_leveraged_etf_name(name: str) -> bool:
    """Return True if a fund's name matches known leveraged/inverse issuer phrasing.
    Best-effort supplement to `is_leveraged_etf_symbol` for products not yet in the
    exact-match list — see `_LEVERAGED_ETF_NAME_KEYWORDS`."""
    lowered = name.lower()
    return any(kw in lowered for kw in _LEVERAGED_ETF_NAME_KEYWORDS)


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
    options_collateral: float = 0.0        # cash/shares currently locked in open options positions (CSP + CC)


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


@dataclass(frozen=True)
class OptionsOrderIntent:
    """A proposed CSP/CC sell-to-open, before the gate. `collateral` is the dollar
    amount this contract would lock up (100 * strike for a CSP; share value for a CC)."""

    underlying: str
    collateral: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "underlying", self.underlying.strip().upper())
        if self.collateral <= 0:
            raise ValueError(f"collateral must be > 0, got {self.collateral}")


class KillSwitch:
    """A file-backed halt. Presence of the file means "stop trading", so it survives a
    process crash and can be tripped out-of-band (touch the file) independent of any UI.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def engaged(self) -> bool:
        return self._path.exists()

    def note(self) -> str | None:
        try:
            return self._path.read_text().strip()
        except FileNotFoundError:
            return None

    def engage(self, note: str = "") -> None:
        # Write-then-replace so a reader never sees a half-written flag.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(note or "engaged")
        os.replace(tmp, self._path)

    def disengage(self) -> None:
        self._path.unlink(missing_ok=True)


class AutonomyOverride:
    """A file-backed manual/auto override for the trading autonomy mode.

    Mirrors KillSwitch: the override lives in a file so it survives a process crash
    and crosses the API/scheduler thread (and standalone-process) boundary — the
    dashboard writes it, the trading loop reads it. Absent file → no override, and
    the loop falls back to the AUTONOMY env value. This is what makes the dashboard
    manual/auto toggle actually change trading behaviour rather than a cosmetic global.
    """

    _VALID = {"manual", "auto"}

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def get(self) -> str | None:
        """The override in force, or None when no override file is present.

        Fail-safe: a file that exists but is unreadable or holds an unrecognized
        value resolves to "manual", never None. Returning None would let
        effective_autonomy fall back to config.autonomy (possibly "auto") — a
        corrupted brake must not silently release into autonomous trading.
        """
        try:
            value = self._path.read_text().strip().lower()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("autonomy override unreadable (%s) — forcing manual", exc)
            return "manual"
        if value in self._VALID:
            return value
        logger.warning("autonomy override file has invalid content %r — forcing manual", value)
        return "manual"

    def set(self, mode: str) -> None:
        mode = mode.strip().lower()
        if mode not in self._VALID:
            raise ValueError(f"invalid autonomy mode: {mode!r}")
        # Write-then-replace so a reader never sees a half-written flag.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(mode)
        os.replace(tmp, self._path)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


def effective_autonomy(config) -> str:
    """The autonomy mode actually in force: file override if set, else config.autonomy.

    Every decision-gate branch (manual → queue proposal, auto → submit) must read
    this, not config.autonomy directly, or the dashboard toggle does nothing.
    """
    path = getattr(config, "autonomy_override_path", None)
    override = AutonomyOverride(path).get() if path else None
    return override or config.autonomy


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
        #     Round-trip cost ≈ 2 × spread_pct. When require_spread_data is enabled,
        #     missing spread is treated as fail-closed rather than skipped.
        if intent.side == "buy":
            if intent.spread_pct > 0.0:
                round_trip_cost = 2.0 * intent.spread_pct
                if round_trip_cost > limits.max_spread_pct:
                    return RiskDecision.reject(
                        f"spread too wide: round-trip cost {round_trip_cost:.3%} "
                        f"> max {limits.max_spread_pct:.3%}"
                    )
            elif limits.require_spread_data:
                return RiskDecision.reject(
                    "spread data missing and require_spread_data enabled"
                )

        # 1. Allowlist — route by asset type.
        # None means dynamic/open universe; skip the check entirely.
        _is_crypto = is_crypto_symbol(intent.symbol)
        _active_allowlist = limits.crypto_allowlist if _is_crypto else limits.allowlist
        if _active_allowlist is not None and intent.symbol not in _active_allowlist:
            return RiskDecision.reject(f"{intent.symbol} not in allowlist")

        # 1b. Equity buy hard gates — leveraged/inverse ETF blocklist + minimum price.
        #     Sells always pass (must be able to exit an existing/legacy position);
        #     options and crypto are out of scope for both checks.
        if intent.side == "buy" and not _is_crypto and not is_option_symbol(intent.symbol):
            if limits.block_leveraged_etfs and is_leveraged_etf_symbol(intent.symbol):
                return RiskDecision.reject(
                    f"{intent.symbol} is a leveraged/inverse ETF — blocked (reverse-split risk)"
                )
            if intent.ref_price < limits.min_equity_price:
                return RiskDecision.reject(
                    f"{intent.symbol} price ${intent.ref_price:.2f} below minimum "
                    f"${limits.min_equity_price:.2f}"
                )

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
            # Sizing a sell down to zero is the only true no-op; any positive
            # notional for an existing position is an exit and must be allowed.
            if approved <= 0.0:
                return RiskDecision.reject("approved notional below minimum")
            return RiskDecision.approve(approved, "sell approved")

        # 5b. Daily-loss halt (buys only, opt-in). Blocks NEW buys once the day's drawdown
        #     hits the limit; sells already returned above, so exits are never blocked.
        #     If require_daily_pnl_check is enabled, an unknown P&L must fail closed
        #     (CCXT can opt out by setting require_daily_pnl_check=False).
        if limits.daily_loss_halt_enabled:
            if state.daily_pnl_pct is None:
                if limits.require_daily_pnl_check:
                    return RiskDecision.reject(
                        "daily P&L unknown and require_daily_pnl_check enabled — new buys halted"
                    )
            elif state.daily_pnl_pct <= -limits.daily_loss_limit_pct:
                return RiskDecision.reject(
                    f"daily loss {state.daily_pnl_pct:.2%} hit limit "
                    f"-{limits.daily_loss_limit_pct:.2%} — new buys halted"
                )

        # 6. Max position size (buys only) — cap is a fraction of pool equity, not total equity.
        #    Also subtract capital already deployed this tick in the same pool so a
        #    single tick cannot over-commit the account.
        if state.equity <= 0.0:
            return RiskDecision.reject("account equity non-positive — cannot size new positions")
        _cap_pct = limits.max_crypto_position_pct if _is_crypto else limits.max_position_pct
        _pool_fraction = (
            limits.intraday_pool_pct
            if intent.pool == "intraday"
            else (1.0 - limits.intraday_pool_pct)
        )
        cap = _cap_pct * state.equity * _pool_fraction
        _already_deployed = (
            state.intraday_deployed
            if intent.pool == "intraday"
            else state.deployed_notional
        )
        cap -= _already_deployed
        existing_value = held * intent.ref_price
        headroom = cap - existing_value
        if headroom <= _NO_OP_EPSILON:
            if _already_deployed > 0 and cap <= _NO_OP_EPSILON:
                return RiskDecision.reject(
                    f"{intent.pool} pool already deployed ${_already_deployed:,.0f} "
                    f"this tick (cap ${cap + _already_deployed:,.0f})"
                )
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

    def evaluate_options_order(
        self,
        intent: OptionsOrderIntent,
        state: AccountState,
        kill_switch: KillSwitch | None = None,
    ) -> RiskDecision:
        """Gate a CSP/CC sell-to-open. Two caps, both must pass:

        1. Options-only cap: existing options collateral + this contract stays within
           `max_options_allocation_pct` of equity.
        2. Combined cap: total commitment across stock/crypto positions + options
           collateral must not exceed equity — a backstop against the three asset
           classes independently sizing to 100% each and jointly over-committing.
        """
        limits = self._limits

        if kill_switch is not None and kill_switch.engaged():
            return RiskDecision.reject("kill switch engaged")
        if state.stale:
            return RiskDecision.reject("account state stale (reconciliation failed)")

        # Daily-loss halt — mirrors the buy-side check in evaluate(). CSP/CC
        # sell-to-open commits new capital, so the same halt should apply.
        if limits.daily_loss_halt_enabled:
            if state.daily_pnl_pct is None:
                if limits.require_daily_pnl_check:
                    return RiskDecision.reject(
                        "daily P&L unknown and require_daily_pnl_check enabled — "
                        "new options orders halted"
                    )
            elif state.daily_pnl_pct <= -limits.daily_loss_limit_pct:
                return RiskDecision.reject(
                    f"daily loss {state.daily_pnl_pct:.2%} hit limit "
                    f"-{limits.daily_loss_limit_pct:.2%} — new options orders halted"
                )

        # Cash check — CSP collateral must be available in uninvested cash. Without
        # this, a CSP could be accepted while the account has insufficient liquidity
        # to cover assignment (the broker would reject it, but we want to fail here).
        if intent.collateral > state.cash:
            return RiskDecision.reject(
                f"insufficient cash: collateral ${intent.collateral:,.0f} "
                f"> available cash ${state.cash:,.0f}"
            )

        options_cap = limits.max_options_allocation_pct * state.equity
        options_after = state.options_collateral + intent.collateral
        if options_after > options_cap:
            return RiskDecision.reject(
                f"options allocation ${options_after:,.0f} would exceed cap "
                f"${options_cap:,.0f} ({limits.max_options_allocation_pct:.0%} of equity)"
            )

        # Fail closed on equities only: if we don't know the cost basis of a held
        # stock/ETF, we cannot prove the combined cap is safe. Crypto positions with no
        # recorded avg_entry_price are excluded from this hard block because Alpaca
        # sometimes does not provide a cost basis for crypto, but options are not
        # available on crypto anyway.
        missing_price_symbols = [
            sym for sym, qty in state.positions.items()
            if qty != 0
            and not is_crypto_symbol(sym)
            and state.avg_entry_prices.get(sym, 0.0) <= 0
        ]
        if missing_price_symbols:
            logger.warning(
                "options gate: missing avg_entry_price for %s — cannot verify combined cap",
                missing_price_symbols,
            )
            return RiskDecision.reject(
                "cannot verify combined cap: missing avg_entry_price for held positions"
            )

        stock_crypto_exposure = sum(
            qty * state.avg_entry_prices.get(sym, 0.0) for sym, qty in state.positions.items()
        )
        combined_after = stock_crypto_exposure + options_after
        if combined_after > state.equity:
            return RiskDecision.reject(
                f"combined exposure ${combined_after:,.0f} (stock/crypto "
                f"${stock_crypto_exposure:,.0f} + options ${options_after:,.0f}) "
                f"would exceed equity ${state.equity:,.0f}"
            )

        return RiskDecision.approve(intent.collateral, "options order approved")
