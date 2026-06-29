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
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Protocol

from trader.config import Config, load_config
from trader.risk.gate import AccountState

logger = logging.getLogger(__name__)

# ponytail: full reconcile at most every 5 min when trade stream is running
_RECONCILE_CACHE_TTL = 300.0

Side = str  # "buy" | "sell"


def client_order_id_for(
    trade_date: date, symbol: str, side: Side, strategy_name: str
) -> str:
    """Deterministic id for one logical decision. Keyed on decision identity — NOT on a
    run id — so a retry in a later run reuses the same id and cannot double-fire."""
    key = f"{trade_date.isoformat()}|{symbol.upper()}|{side}|{strategy_name}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


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
    def cancel_order_by_id(self, order_id: str) -> None: ...


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
        self._cached_state: AccountState | None = None
        self._cache_ts: float = 0.0
        self._state_lock = threading.Lock()
        self._stream_started = False

    # ---- trade stream (event-driven cache invalidation) ----

    def start_trade_stream(self) -> None:
        """Start Alpaca WebSocket trade-update stream in a daemon thread.

        On fill/cancel events the reconcile cache is invalidated so the next
        tick does a fresh API pull instead of waiting up to 5 minutes.
        No-op if already started or if Alpaca keys are not configured.
        """
        if self._stream_started or not self._config.alpaca_api_key:
            return
        self._stream_started = True
        t = threading.Thread(target=self._run_stream, daemon=True, name="alpaca-trade-stream")
        t.start()
        logger.info("trade stream started")

    def _run_stream(self) -> None:
        # Reconnect loop: Alpaca paper-trading WebSocket drops after ~1 hour.
        # Restart immediately on clean exit; back off 30 s on error.
        while True:
            try:
                from alpaca.trading.stream import TradingStream
                self._config.require_alpaca()
                ts = TradingStream(
                    api_key=self._config.alpaca_api_key,
                    secret_key=self._config.alpaca_secret_key,
                    paper=self._config.alpaca_paper,
                )
                broker = self

                @ts.subscribe_trade_updates
                async def _on_update(data: Any) -> None:
                    event = str(getattr(data, "event", "")).lower()
                    symbol = str(getattr(getattr(data, "order", None), "symbol", "?"))
                    logger.info("trade stream: event=%s symbol=%s — invalidating cache", event, symbol)
                    broker._invalidate_cache()

                ts.run()
                logger.info("trade stream exited cleanly — reconnecting in 5s")
                time.sleep(5)
            except Exception:
                logger.exception("trade stream error — reconnecting in 30s")
                self._stream_started = False
                time.sleep(30)
                self._stream_started = True

    def _invalidate_cache(self) -> None:
        with self._state_lock:
            self._cache_ts = 0.0

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
        """Fetch account activities as plain dicts. alpaca-py's TradingClient does not
        consistently expose this endpoint across SDK versions. Returns [] on any error
        — callers must handle empty list.

        Each returned dict: {"symbol", "side", "qty", "price", "ts", "order_id"}.
        "order_id" is Alpaca's own order id — matches our orders.broker_order_id,
        not client_order_id.
        """
        import requests

        try:
            self._config.require_alpaca()
            url = (
                f"{self._config.alpaca_base_url}"
                f"/v2/account/activities/{activity_type}"
            )
            resp = requests.get(
                url,
                headers={
                    "APCA-API-KEY-ID": self._config.alpaca_api_key or "",
                    "APCA-API-SECRET-KEY": self._config.alpaca_secret_key or "",
                },
                timeout=15,
            )
            resp.raise_for_status()
            activities = resp.json()

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
                        "order_id": a.get("order_id"),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
            return result
        except Exception as exc:
            logger.warning("get_account_activities failed: %s", exc)
            return []

    # ---- reconciliation (source of truth) ----

    def reconcile(self, *, trades_today_override: int | None = None) -> AccountState:
        """Return live account state. Uses a 5-minute cache when the trade stream is
        running; the stream invalidates the cache on every fill/cancel so the next
        tick always sees fresh state after a trade event."""
        if trades_today_override is None and self._stream_started:
            with self._state_lock:
                if self._cached_state is not None and time.monotonic() - self._cache_ts < _RECONCILE_CACHE_TTL:
                    return self._cached_state

        state = self._full_reconcile(trades_today_override=trades_today_override)
        if self._stream_started:
            with self._state_lock:
                self._cached_state = state
                self._cache_ts = time.monotonic()
        return state

    def _full_reconcile(self, *, trades_today_override: int | None = None) -> AccountState:
        """Full Alpaca API reconciliation. Fails closed on any error."""
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
        """Place an order, exactly one of `notional` (dollars) or `qty` (shares).

        Buys use notional (Alpaca's fractional path). Sells/exits use `qty` — Alpaca
        restricts notional sells. Idempotent: a duplicate client_order_id is swallowed.

        When ORDER_TYPE=limit and ref_price is supplied, buy orders are placed as DAY
        limit orders at the bid/ask mid. Sells always use market orders for reliable exits.
        If a limit buy doesn't fill by EOD it cancels; the next tick retries.

        Non-fractionable assets reject notional orders (Alpaca code 40310000). When
        ref_price is supplied, such rejections retry once as whole-share qty orders.
        """
        if side not in {"buy", "sell"}:
            raise ValueError(f"invalid side: {side!r}")
        if (notional is None) == (qty is None):
            raise ValueError("pass exactly one of notional or qty")
        client = self._ensure_client()

        use_limit = (
            side == "buy"
            and self._config.order_type == "limit"
            and ref_price is not None
            and ref_price > 0
            and "/" not in symbol  # crypto only supports market on Alpaca
        )
        if use_limit:
            request = _build_limit_order_request(
                symbol=symbol, side=side, client_order_id=client_order_id,
                notional=notional, qty=qty, limit_price=ref_price,
            )
        else:
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

    def place_stop_order(
        self,
        *,
        symbol: str,
        qty: float,
        stop_price: float,
        client_order_id: str,
    ) -> Any:
        """Place a GTC stop-market order to protect a long position.

        Idempotent: a duplicate client_order_id returns the existing order.
        Only call for equity symbols — crypto uses separate stop logic.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        whole_qty = int(qty)
        if whole_qty < 1:
            logger.warning("place_stop_order skipped for %s — qty %.4f rounds to 0", symbol, qty)
            return None
        client = self._ensure_client()
        def _build_request(q: int) -> StopOrderRequest:
            return StopOrderRequest(
                symbol=symbol,
                qty=q,  # GTC stops must be whole shares on Alpaca
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
                client_order_id=client_order_id,
            )

        try:
            return self._submit_idempotent(client, _build_request(whole_qty), client_order_id)
        except Exception as exc:
            avail = _insufficient_qty_available(exc)
            if avail and avail >= 1:
                logger.warning(
                    "stop qty %d exceeds available %d for %s; retrying with available",
                    whole_qty, avail, symbol,
                )
                return self._submit_idempotent(client, _build_request(avail), client_order_id)
            raise

    def cancel_open_stops(self, symbol: str) -> None:
        """Cancel open GTC stop-sell orders for symbol. Best-effort — logs failures."""
        try:
            client = self._ensure_client()
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus

            open_orders = client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )
            for order in open_orders:
                order_type = str(
                    getattr(order, "order_type", None) or getattr(order, "type", "")
                ).lower()
                if (
                    order.symbol == symbol
                    and order_type in {"stop", "stop_limit"}
                    and str(getattr(order, "side", "")).lower() == "sell"
                ):
                    try:
                        client.cancel_order_by_id(str(order.id))
                        logger.info("cancelled stop order %s for %s", order.id, symbol)
                    except Exception:
                        logger.warning(
                            "failed to cancel stop order %s for %s", order.id, symbol
                        )
        except Exception:
            logger.exception("cancel_open_stops failed for %s", symbol)

    def _submit_idempotent(
        self, client: _TradingClient, request: Any, client_order_id: str
    ) -> Any:
        """Submit, treating a duplicate client_order_id as success (return existing).

        Handles Alpaca wash-trade rejection (40310000): cancels the conflicting order
        identified in the error payload and retries up to 3 times (multiple conflicting
        orders can exist if cancel_open_stops raced with a new placement).
        """
        import time as _time
        for attempt in range(3):
            try:
                return client.submit_order(order_data=request)
            except Exception as exc:  # noqa: BLE001 - inspect for known recoverable cases
                if _is_duplicate_order(exc):
                    logger.info(
                        "duplicate client_order_id %s; returning existing order",
                        client_order_id,
                    )
                    return client.get_order_by_client_id(client_order_id)
                conflicting_id = _wash_trade_order_id(exc)
                if conflicting_id:
                    logger.warning(
                        "wash-trade rejection for %s (attempt %d) — cancelling %s",
                        client_order_id, attempt + 1, conflicting_id,
                    )
                    try:
                        client.cancel_order_by_id(conflicting_id)
                    except Exception:
                        logger.warning("failed to cancel conflicting order %s", conflicting_id)
                    _time.sleep(1.0)  # Alpaca cancel is async; wait for shares to release
                    continue
                raise
        raise RuntimeError(f"wash-trade retry exhausted for {client_order_id}")


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


def _wash_trade_order_id(exc: Exception) -> str | None:
    """Return the conflicting order ID from an Alpaca wash-trade rejection (40310000), or None."""
    import json
    text = str(exc)
    if "40310000" not in text and "wash trade" not in text.lower():
        return None
    # The APIError string contains the raw JSON payload; extract existing_order_id.
    try:
        start = text.index("{")
        payload = json.loads(text[start:])
        return payload.get("existing_order_id")
    except (ValueError, KeyError):
        return None


def _insufficient_qty_available(exc: Exception) -> int | None:
    """Return available int qty from Alpaca 'insufficient qty' error payload, or None."""
    import json
    text = str(exc)
    if "insufficient qty" not in text.lower():
        return None
    try:
        start = text.index("{")
        payload = json.loads(text[start:])
        val = payload.get("available")
        return int(val) if val is not None else None
    except (ValueError, KeyError):
        return None


def _is_not_fractionable(exc: Exception) -> bool:
    """True when Alpaca rejected a notional order on a non-fractionable asset
    (code 40310000, message 'asset "X" is not fractionable')."""
    return "not fractionable" in str(exc).lower()


def _build_limit_order_request(
    *,
    symbol: str,
    side: Side,
    client_order_id: str,
    notional: float | None = None,
    qty: float | None = None,
    limit_price: float,
) -> Any:
    """Build a DAY limit order at `limit_price` (bid/ask mid). Equity buys only."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    return LimitOrderRequest(
        symbol=symbol,
        notional=round(notional, 2) if notional is not None else None,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_price, 2),
        client_order_id=client_order_id,
    )


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

    # Alpaca crypto only accepts GTC; equity accepts DAY
    tif = TimeInForce.GTC if "/" in symbol else TimeInForce.DAY
    return MarketOrderRequest(
        symbol=symbol,
        notional=round(notional, 2) if notional is not None else None,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=tif,
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
        status=QueryOrderStatus.CLOSED, after=midnight, limit=500
    )
    return open_filter, closed_today_filter
