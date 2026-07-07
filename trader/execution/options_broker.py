"""Alpaca options broker adapter — contract selection + CSP/CC order submission.

Sibling to `AlpacaBroker` (`trader/execution/broker.py`), not a shared subclass — the
codebase has no common `Broker` interface today, so this follows the same pattern:
wrap a minimal client Protocol, keep the SDK behind an injectable seam, fail closed on
reconciliation errors.

CONTRACT SIZING IS NOT A NOTIONAL SWAP. Equity buys size to a fractional dollar amount;
option contracts are quantized to 100 shares of collateral per contract, so this module
picks a strike + whole contract count that fits a target collateral budget, and returns
None if even one contract would exceed it (the pipeline skips the trade rather than
oversize — see `select_csp_contract` / `select_cc_contract`).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from trader.config import Config, load_config

logger = logging.getLogger(__name__)

Right = str  # "call" | "put"


class ContractCandidate:
    """Plain data carrier for one option contract — decouples callers from the SDK's
    OptionContract model."""

    __slots__ = ("symbol", "strike", "expiry", "open_interest")

    def __init__(self, symbol: str, strike: float, expiry: date, open_interest: int) -> None:
        self.symbol = symbol
        self.strike = strike
        self.expiry = expiry
        self.open_interest = open_interest


def options_client_order_id_for(
    trade_date: date, underlying: str, right: Right, strategy_name: str
) -> str:
    """Deterministic id for one logical options decision — mirrors
    `broker.client_order_id_for`, keyed on decision identity so a retry cannot double-fire."""
    key = f"{trade_date.isoformat()}|{underlying.upper()}|{right}|{strategy_name}|opt"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


class _OptionsTradingClient(Protocol):
    """The slice of alpaca-py's TradingClient this adapter uses."""

    def get_option_contracts(self, request: Any) -> Any: ...
    def submit_order(self, order_data: Any) -> Any: ...
    def get_order_by_client_id(self, client_id: str) -> Any: ...
    def get_all_positions(self) -> list[Any]: ...
    def exercise_options_position(self, symbol_or_id: str) -> None: ...


class _OptionsDataClient(Protocol):
    """The slice of alpaca-py's OptionHistoricalDataClient this adapter uses."""

    def get_option_latest_quote(self, request: Any) -> Any: ...


class AlpacaOptionsBroker:
    """Wraps Alpaca's options contracts + order-submission API. Inject `client` /
    `data_client` in tests; built lazily from `config` in production."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        client: _OptionsTradingClient | None = None,
        data_client: _OptionsDataClient | None = None,
    ) -> None:
        self._config = config or load_config()
        self._client = client
        self._data_client = data_client

    def _ensure_client(self) -> _OptionsTradingClient:
        if self._client is None:
            from alpaca.trading.client import TradingClient

            self._config.require_alpaca()
            self._client = TradingClient(
                api_key=self._config.alpaca_api_key,
                secret_key=self._config.alpaca_secret_key,
                paper=self._config.alpaca_options_paper,
            )
        return self._client

    def _ensure_data_client(self) -> _OptionsDataClient:
        if self._data_client is None:
            from alpaca.data.historical.option import OptionHistoricalDataClient

            self._config.require_alpaca()
            self._data_client = OptionHistoricalDataClient(
                api_key=self._config.alpaca_api_key,
                secret_key=self._config.alpaca_secret_key,
            )
        return self._data_client

    # ---- chain lookup / liquidity filter ----

    def eligible_chain(
        self, underlying: str, right: Right, *, min_dte: int = 20, max_dte: int = 45,
    ) -> list[ContractCandidate]:
        """Contracts for `underlying` expiring in [min_dte, max_dte] days, filtered to
        `options_min_open_interest`. Empty list means "not options-eligible right now" —
        callers must skip the symbol rather than pick an illiquid strike."""
        from alpaca.trading.enums import ContractType
        from alpaca.trading.requests import GetOptionContractsRequest

        client = self._ensure_client()
        today = datetime.now(timezone.utc).date()
        request = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            type=ContractType.PUT if right == "put" else ContractType.CALL,
            expiration_date_gte=today + timedelta(days=min_dte),
            expiration_date_lte=today + timedelta(days=max_dte),
        )
        try:
            response = client.get_option_contracts(request)
        except Exception:
            logger.exception("get_option_contracts failed for %s", underlying)
            return []

        min_oi = self._config.risk.options_min_open_interest
        candidates: list[ContractCandidate] = []
        for c in response.option_contracts or []:
            oi = int(c.open_interest) if c.open_interest else 0
            if oi < min_oi:
                continue
            candidates.append(
                ContractCandidate(
                    symbol=c.symbol, strike=float(c.strike_price),
                    expiry=c.expiration_date, open_interest=oi,
                )
            )
        return candidates

    def check_spread(self, contract_symbol: str) -> float | None:
        """Bid-ask spread as a fraction of mid, or None if quote unavailable."""
        from alpaca.data.requests import OptionLatestQuoteRequest

        try:
            data_client = self._ensure_data_client()
            resp = data_client.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=contract_symbol)
            )
            quote = resp[contract_symbol]
            bid, ask = float(quote.bid_price), float(quote.ask_price)
            if bid <= 0 or ask <= 0:
                return None
            mid = (bid + ask) / 2.0
            return (ask - bid) / mid
        except Exception:
            logger.warning("check_spread failed for %s", contract_symbol)
            return None

    # ---- contract selection (whole-contract sizing, not a notional swap) ----

    def select_csp_contract(
        self, underlying: str, ref_price: float, max_collateral: float,
    ) -> ContractCandidate | None:
        """Pick the highest-strike OTM put (closest to the money, richer premium) whose
        100x collateral fits within `max_collateral`. None if no contract fits — the
        caller must skip the trade rather than undersize below one contract."""
        candidates = [
            c for c in self.eligible_chain(underlying, "put")
            if c.strike < ref_price and c.strike * 100.0 <= max_collateral
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.strike)

    def select_cc_contract(
        self, underlying: str, ref_price: float, shares_held: float,
    ) -> ContractCandidate | None:
        """Pick the lowest-strike OTM call (closest to the money) covered by
        `shares_held`. Requires >= 100 shares; None if under-covered or no contract."""
        if shares_held < 100:
            return None
        candidates = [c for c in self.eligible_chain(underlying, "call") if c.strike > ref_price]
        if not candidates:
            return None
        return min(candidates, key=lambda c: c.strike)

    # ---- order submission (idempotent, mirrors AlpacaBroker.submit) ----

    def sell_to_open(self, *, contract_symbol: str, client_order_id: str) -> Any:
        """Sell one contract to open (CSP or CC). Idempotent on client_order_id."""
        from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        client = self._ensure_client()
        request = MarketOrderRequest(
            symbol=contract_symbol, qty=1, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, client_order_id=client_order_id,
            position_intent=PositionIntent.SELL_TO_OPEN,
        )
        return self._submit_idempotent(client, request, client_order_id)

    def buy_to_close(self, *, contract_symbol: str, client_order_id: str) -> Any:
        """Buy one contract to close early (e.g. to defend against assignment)."""
        from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        client = self._ensure_client()
        request = MarketOrderRequest(
            symbol=contract_symbol, qty=1, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, client_order_id=client_order_id,
            position_intent=PositionIntent.BUY_TO_CLOSE,
        )
        return self._submit_idempotent(client, request, client_order_id)

    def _submit_idempotent(
        self, client: _OptionsTradingClient, request: Any, client_order_id: str
    ) -> Any:
        try:
            return client.submit_order(order_data=request)
        except Exception as exc:  # noqa: BLE001 - duplicate id is a normal retry path
            text = str(exc).lower()
            if "client_order_id" in text and ("exist" in text or "duplicate" in text or "unique" in text):
                logger.info("duplicate options client_order_id %s; returning existing order", client_order_id)
                return client.get_order_by_client_id(client_order_id)
            raise

    # ---- reconciliation (assignment/expiry detection) ----

    def open_option_positions(self) -> list[dict]:
        """Current option positions held at the broker, as plain dicts. Used to detect
        assignment/expiry that happened off-hours — compare against `options_positions`
        rows the repository thinks are still open."""
        client = self._ensure_client()
        try:
            positions = client.get_all_positions()
        except Exception:
            logger.exception("get_all_positions failed for options reconciliation")
            return []
        return [
            {"symbol": p.symbol, "qty": float(p.qty), "asset_class": str(getattr(p, "asset_class", ""))}
            for p in positions
            if str(getattr(p, "asset_class", "")).lower() == "us_option"
        ]
