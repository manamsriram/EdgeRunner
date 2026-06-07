"""CCXT broker adapter — multi-exchange crypto execution.

Same interface as AlpacaBroker (reconcile + submit) so the pipeline and risk gate
need no changes to work with CCXT exchanges.

Two design guarantees mirror AlpacaBroker:

  1. FAIL-CLOSED RECONCILE. Any error in reconcile() returns stale=True so the risk
     gate refuses all trades on unknown account state.

  2. NOTIONAL→QTY CONVERSION. The pipeline always passes notional (USD amount) for
     buys. CCXT requires qty (base currency). submit() converts via a live ticker
     fetch so the conversion reflects the current market price.

IMPORTANT: CCXTBroker.reconcile() always returns daily_pnl_pct=None because CCXT
exchanges do not expose a "last_equity" baseline equivalent to Alpaca's account
snapshot. Callers MUST use RiskLimits(require_daily_pnl_check=False) with this broker;
the daily-loss circuit breaker is skipped rather than blocking all trades.
"""
from __future__ import annotations

import logging
from typing import Any

from trader.config import Config, load_config
from trader.risk.gate import AccountState

logger = logging.getLogger(__name__)


class CCXTBroker:
    """Wraps a CCXT exchange instance. Inject `exchange` in tests; in production it
    is built lazily from config on first use."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        exchange: Any | None = None,
    ) -> None:
        self._config = config or load_config()
        self._exchange = exchange

    # ---- client lifecycle ----

    def _ensure_exchange(self) -> Any:
        if self._exchange is None:
            import ccxt

            name = self._config.crypto_exchange
            exchange_cls = getattr(ccxt, name, None)
            if exchange_cls is None:
                raise RuntimeError(
                    f"Unknown CCXT exchange: {name!r}. "
                    "Set CRYPTO_EXCHANGE to a valid ccxt exchange name."
                )
            self._exchange = exchange_cls(
                {
                    "apiKey": self._config.ccxt_api_key or "",
                    "secret": self._config.ccxt_secret_key or "",
                }
            )
        return self._exchange

    # ---- reconciliation (source of truth) ----

    def reconcile(self) -> AccountState:
        """Read live account state. Fails closed on any error.

        daily_pnl_pct is always None — CCXT has no last_equity baseline.
        Callers must configure RiskLimits(require_daily_pnl_check=False).
        """
        try:
            ex = self._ensure_exchange()
            balance = ex.fetch_balance()

            # Aggregate total holdings as a symbol→qty map.
            positions: dict[str, float] = {
                sym: float(qty)
                for sym, qty in balance.get("total", {}).items()
                if qty and float(qty) > 0
            }

            # Equity approximated as USDT balance (or USD if available).
            # More accurate: sum(qty * price) for each held asset, but requires
            # n ticker calls — USDT total is a reasonable conservative estimate.
            equity = float(
                balance.get("total", {}).get("USDT")
                or balance.get("total", {}).get("USD")
                or 0.0
            )

            open_orders = ex.fetch_open_orders()
            open_symbols = frozenset(str(o.get("symbol", "")) for o in open_orders)

            return AccountState(
                equity=equity,
                positions=positions,
                open_order_symbols=open_symbols,
                trades_today=0,        # CCXT doesn't expose a daily fill count
                daily_pnl_pct=None,    # no last_equity baseline on CCXT
                stale=False,
            )
        except Exception:
            logger.exception("CCXTBroker reconcile failed; returning stale state")
            return AccountState(
                equity=0.0,
                positions={},
                open_order_symbols=frozenset(),
                trades_today=0,
                daily_pnl_pct=None,
                stale=True,
            )

    # ---- order submission ----

    def submit(
        self,
        *,
        symbol: str,
        side: str,
        client_order_id: str,
        notional: float | None = None,
        qty: float | None = None,
    ) -> Any:
        """Place a market order. Accepts same interface as AlpacaBroker.

        Buys may pass notional (USD); qty is derived from a live ticker fetch.
        Sells should pass qty directly (position size to close).
        client_order_id is stored as a note/tag where the exchange supports it.
        """
        if side not in {"buy", "sell"}:
            raise ValueError(f"invalid side: {side!r}")
        if notional is None and qty is None:
            raise ValueError("pass at least one of notional or qty")

        ex = self._ensure_exchange()

        if notional is not None and qty is None:
            ticker = ex.fetch_ticker(symbol)
            price = float(ticker.get("last") or 0.0)
            if price <= 0:
                raise RuntimeError(
                    f"Cannot convert notional to qty: invalid price {price} for {symbol}"
                )
            qty = notional / price

        params: dict[str, Any] = {}
        if ex.has.get("createOrderWithClientOrderId"):
            params["clientOrderId"] = client_order_id

        logger.info(
            "CCXTBroker submit symbol=%s side=%s qty=%.6f client_order_id=%s",
            symbol, side, qty, client_order_id,
        )
        return ex.create_market_order(symbol, side, qty, params=params)
