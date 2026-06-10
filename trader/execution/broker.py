"""Alpaca broker adapter — reconciliation + idempotent order submission.

Two guarantees live here:

  1. BROKER IS SOURCE OF TRUTH. `reconcile()` returns the whole `AccountState` the risk
     gate needs, read live from Alpaca, never from local state. On *any* error it returns
     `stale=True` so the gate fails closed rather than trading on an unknown account.

  2. IDEMPOTENT ORDERS. Every submit carries a `client_order_id` derived from the
     *decision identity* (date|symbol|side|strategy) — stable across retries and process
     restarts. If Alpaca rejects a duplicate id, we treat it as success and return the
     existing order, so a retry observes one order, never two.

Every Alpaca SDK touch sits behind an injectable seam (`client`, `request_builder`,
`order_filter_builder`), so this module and its tests import no SDK and hit no network.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timezone
from typing import Any, Callable, Protocol

from trader.config import Config, load_config
from trader.risk.gate import AccountState

logger = logging.getLogger(__name__)

Side = str  # "buy" | "sell"


def client_order_id_for(
    trade_date: date, symbol: str, side: Side, strategy_name: str
) -> str:
    """Deterministic id for one logical decision. Keyed on decision identity — NOT on a
    run id — so a retry in a later run reuses the same id and cannot double-fire."""
    key = f"{trade_date.isoformat()}|{symbol.upper()}|{side}|{strategy_name}"
    return hashlib.sha1(key.encode()).hexdigest()[:32]


class _TradingClient(Protocol):
    """The slice of alpaca-py's TradingClient this adapter uses (kept minimal so fakes
    are trivial to write in tests)."""

    def get_account(self) -> Any: ...
    def get_all_positions(self) -> list[Any]: ...
    def get_orders(self, filter: Any) -> list[Any]: ...  # noqa: A002 - alpaca's kw name
    def submit_order(self, order_data: Any) -> Any: ...
    def get_order_by_client_id(self, client_id: str) -> Any: ...
    def get_portfolio_history(self, history_filter: Any = None) -> Any: ...
    def get_account_activities(self, filter: Any = None) -> Any: ...


class AlpacaBroker:
    """Wraps a TradingClient. Inject `client` (and optionally the SDK builders) in tests;
    in production they are built lazily from `config` on first use."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        client: _TradingClient | None = None,
        request_builder: Callable[..., Any] | None = None,
        order_filter_builder: Callable[[date], tuple[Any, Any]] | None = None,
    ) -> None:
        self._config = config or load_config()
        self._client = client
        self._request_builder = request_builder or _build_market_order_request
        self._order_filter_builder = order_filter_builder or _build_order_filters

    # ---- client lifecycle ----

    def _ensure_client(self) -> _TradingClient:
        if self._client is None:
            from alpaca.trading.client import TradingClient

            self._config.require_alpaca()
            self._client = TradingClient(
                api_key=self._config.alpaca_api_key,
                secret_key=self._config.alpaca_secret_key,
                paper=self._config.alpaca_paper,
            )
        return self._client

    def get_positions(self) -> list[dict]:
        """Return current positions as plain dicts (decouples callers from SDK objects)."""
        client = self._ensure_client()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(getattr(p, "avg_entry_price", 0) or 0),
                "market_value": float(getattr(p, "market_value", 0) or 0),
                "unrealized_pl": float(getattr(p, "unrealized_pl", 0) or 0),
            }
            for p in client.get_all_positions()
        ]

    def get_portfolio_history(self, period: str = "1A") -> dict | None:
        """Return {"timestamp": [...ISO strings...], "equity": [...floats...]} or None.

        `period` follows Alpaca's convention: "1D", "1W", "1M", "3M", "6M", "1A".
        Defaults to "1A" so callers get a full year of daily equity data for Sharpe
        and drawdown computation. The /api/portfolio/history endpoint uses the default.
        """
        try:
            client = self._ensure_client()
            from alpaca.trading.requests import GetPortfolioHistoryRequest
            request = GetPortfolioHistoryRequest(period=period, timeframe="1D")
            history = client.get_portfolio_history(history_filter=request)

            def _ts_to_iso(t: Any) -> str:
                if hasattr(t, "isoformat"):
                    return t.isoformat()
                if isinstance(t, (int, float)):
                    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
                return str(t)

            # Filter out None equity entries (weekends / non-trading periods).
            pairs = [
                (t, e)
                for t, e in zip(history.timestamp, history.equity)
                if e is not None
            ]
            if not pairs:
                return None
            timestamps, equities = zip(*pairs)
            return {
                "timestamp": [_ts_to_iso(t) for t in timestamps],
                "equity": list(equities),
            }
        except Exception as exc:
            logger.warning("get_portfolio_history failed: %s", exc)
            return None

    def get_account_activities(self, activity_type: str = "FILL") -> list[dict]:
        """Fetch account activities as plain dicts. Uses urllib (stdlib) because
        alpaca-py's TradingClient does not consistently expose this endpoint across
        SDK versions. Returns [] on any error — callers must handle empty list.

        Each returned dict: {"symbol", "side", "qty", "price", "ts"}.
        """
        import json
        import urllib.request

        try:
            self._config.require_alpaca()
            url = (
                f"{self._config.alpaca_base_url}"
                f"/v2/account/activities/{activity_type}"
            )
            req = urllib.request.Request(
                url,
                headers={
                    "APCA-API-KEY-ID": self._config.alpaca_api_key or "",
                    "APCA-API-SECRET-KEY": self._config.alpaca_secret_key or "",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                activities = json.loads(resp.read())

            result = []
            for a in activities:
                try:
                    if a.get("activity_type") != activity_type:
                        continue
                    result.append({
                        "symbol": a["symbol"],
                        "side": a["side"].lower(),
                        "qty": float(a["qty"]),
                        "price": float(a["price"]),
                        "ts": a.get("transaction_time", ""),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
            return result
        except Exception as exc:
            logger.warning("get_account_activities failed: %s", exc)
            return []

    # ---- reconciliation (source of truth) ----

    def reconcile(self, *, trades_today_override: int | None = None) -> AccountState:
        """Read live account state into the gate's input shape. Fails closed on error."""
        try:
            client = self._ensure_client()
            account = client.get_account()
            equity = float(account.equity)
            cash = float(getattr(account, "cash", 0.0) or 0.0)
            last_equity = float(getattr(account, "last_equity", 0.0) or 0.0)

            all_positions = client.get_all_positions()
            positions = {p.symbol: float(p.qty) for p in all_positions}
            avg_entry_prices = {
                p.symbol: float(getattr(p, "avg_entry_price", 0) or 0)
                for p in all_positions
            }

            today = datetime.now(timezone.utc).date()
            open_filter, closed_today_filter = self._order_filter_builder(today)
            open_orders = client.get_orders(filter=open_filter)
            closed_today = client.get_orders(filter=closed_today_filter)
            open_order_symbols = frozenset(o.symbol for o in open_orders)
            trades_today = (
                trades_today_override
                if trades_today_override is not None
                else sum(1 for o in closed_today if _is_filled(o))
            )

            # last_equity is prior trading-day close equity; 0/unknown => unprovable.
            daily_pnl_pct = (
                (equity - last_equity) / last_equity if last_equity > 0 else None
            )

            return AccountState(
                equity=equity,
                positions=positions,
                open_order_symbols=open_order_symbols,
                trades_today=trades_today,
                daily_pnl_pct=daily_pnl_pct,
                stale=False,
                cash=cash,
                avg_entry_prices=avg_entry_prices,
            )
        except Exception:  # noqa: BLE001 - any failure must fail closed, not crash trading
            logger.exception("reconcile failed; returning stale account state")
            return AccountState(
                equity=0.0,
                positions={},
                open_order_symbols=frozenset(),
                trades_today=0,
                daily_pnl_pct=None,
                stale=True,
            )

    # ---- order submission (idempotent) ----

    def submit(
        self,
        *,
        symbol: str,
        side: Side,
        client_order_id: str,
        notional: float | None = None,
        qty: float | None = None,
        ref_price: float | None = None,
    ) -> Any:
        """Place a market order, exactly one of `notional` (dollars) or `qty` (shares).

        Buys use notional (Alpaca's fractional path). Sells/exits use `qty` — Alpaca
        restricts *notional* sells, and a long/flat exit closes the held quantity. Idem-
        potent: a duplicate `client_order_id` is swallowed and the existing order returned.

        Non-fractionable assets reject notional orders (Alpaca code 40310000). When
        `ref_price` is supplied, such a rejection retries once as a whole-share qty
        order (floor(notional / ref_price)), reusing the same client_order_id. If even
        one share exceeds the notional, the original rejection propagates.
        """
        if side not in {"buy", "sell"}:
            raise ValueError(f"invalid side: {side!r}")
        if (notional is None) == (qty is None):
            raise ValueError("pass exactly one of notional or qty")
        client = self._ensure_client()
        request = self._request_builder(
            symbol=symbol, side=side, client_order_id=client_order_id,
            notional=notional, qty=qty,
        )
        try:
            return self._submit_idempotent(client, request, client_order_id)
        except Exception as exc:  # noqa: BLE001 - inspect for the non-fractionable case only
            whole_qty = (
                float(int(notional // ref_price))
                if notional is not None and ref_price is not None and ref_price > 0
                else 0.0
            )
            if not (_is_not_fractionable(exc) and whole_qty >= 1.0):
                raise
            logger.info(
                "asset %s not fractionable; retrying %s as %d whole shares",
                symbol, client_order_id, int(whole_qty),
            )
            request = self._request_builder(
                symbol=symbol, side=side, client_order_id=client_order_id,
                notional=None, qty=whole_qty,
            )
            return self._submit_idempotent(client, request, client_order_id)

    def _submit_idempotent(
        self, client: _TradingClient, request: Any, client_order_id: str
    ) -> Any:
        """Submit, treating a duplicate client_order_id as success (return existing)."""
        try:
            return client.submit_order(order_data=request)
        except Exception as exc:  # noqa: BLE001 - inspect for the duplicate case only
            if _is_duplicate_order(exc):
                logger.info(
                    "duplicate client_order_id %s; returning existing order",
                    client_order_id,
                )
                return client.get_order_by_client_id(client_order_id)
            raise


def _is_filled(order: Any) -> bool:
    status = str(getattr(order, "status", "")).lower()
    return "filled" in status


def _is_duplicate_order(exc: Exception) -> bool:
    """Return True only when Alpaca signals a reused client_order_id. Production
    Alpaca phrases this 'client_order_id must be unique' (code 40010001); older
    phrasings use 'exists'/'duplicate'. Unrelated validation errors are not caught."""
    text = str(exc).lower()
    return "client_order_id" in text and (
        "exist" in text or "duplicate" in text or "unique" in text
    )


def _is_not_fractionable(exc: Exception) -> bool:
    """True when Alpaca rejected a notional order on a non-fractionable asset
    (code 40310000, message 'asset "X" is not fractionable')."""
    return "not fractionable" in str(exc).lower()


def _build_market_order_request(
    *,
    symbol: str,
    side: Side,
    client_order_id: str,
    notional: float | None = None,
    qty: float | None = None,
) -> Any:
    """Build a market order via alpaca-py (lazy import; never hit in tests). Exactly one
    of `notional` (dollars) or `qty` (shares) is set."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    return MarketOrderRequest(
        symbol=symbol,
        notional=round(notional, 2) if notional is not None else None,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
    )


def _build_order_filters(today: date) -> tuple[Any, Any]:
    """Build (open-orders, closed-since-midnight) request filters via alpaca-py."""
    from datetime import datetime as _dt
    from datetime import time as _time

    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    midnight = _dt.combine(today, _time.min, tzinfo=timezone.utc)
    open_filter = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    closed_today_filter = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED, after=midnight
    )
    return open_filter, closed_today_filter
