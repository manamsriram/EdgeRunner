"""Email body builders for trading alerts.

Each alert kind gets its own strategy class for building a detailed email
body; `get_email_body_builder` is the factory that selects one by name. The
short text passed to Slack/the email subject in alerts.py is untouched —
this only controls the (optional) longer email body.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any


class AlertEmailBody(ABC):
    """Strategy interface: builds a detailed email body for one alert kind."""

    @abstractmethod
    def build(self, **ctx: Any) -> str:
        ...


class DailyLossBreakerBody(AlertEmailBody):
    def build(self, **ctx: Any) -> str:
        state = ctx["state"]
        config = ctx["config"]
        asof: datetime = ctx["asof"]
        return (
            "Daily-loss breaker tripped.\n\n"
            f"Daily P&L: {state.daily_pnl_pct:.2%}\n"
            f"Limit: {-config.risk.daily_loss_limit_pct:.2%}\n"
            f"Account equity: ${state.equity:,.2f}\n"
            f"Trades today: {state.trades_today}\n"
            f"Open positions: {len(state.positions)} "
            f"({', '.join(sorted(state.positions)) or 'none'})\n"
            f"As of: {asof.isoformat()}\n\n"
            "New buys are halted for the rest of the day; existing stops remain active."
        )


class LiveQuoteFailureBody(AlertEmailBody):
    def build(self, **ctx: Any) -> str:
        equity_symbols = ctx["equity_symbols"]
        asof: datetime = ctx["asof"]
        return (
            "Live quote fetch failed for this tick.\n\n"
            f"Affected symbols ({len(equity_symbols)}): {', '.join(sorted(equity_symbols))}\n"
            f"As of: {asof.isoformat()}\n\n"
            "Stop-loss checks this tick evaluated against yesterday's close "
            "instead of a live price — no skip or halt was applied, this is "
            "logged so the failure frequency is measurable."
        )


class BrokerStopFailedBody(AlertEmailBody):
    def build(self, **ctx: Any) -> str:
        symbol = ctx["symbol"]
        stop_qty = ctx["stop_qty"]
        stop_price = ctx["stop_price"]
        stop_anchor = ctx["stop_anchor"]
        config = ctx["config"]
        exc = ctx["exc"]
        require_stop = config.risk.require_broker_stop
        return (
            f"Broker-side stop placement failed for {symbol}.\n\n"
            f"Position qty: {stop_qty:.6f}\n"
            f"Intended stop price: ${stop_price:.4f} (anchor ${stop_anchor:.4f})\n"
            f"require_broker_stop: {require_stop}\n"
            f"Error: {exc}\n\n"
            + (
                "The buy for this fill is being cancelled to avoid an "
                "unprotected position (require_broker_stop is enabled)."
                if require_stop else
                "The position is still protected by the in-process "
                "software stop-loss until the next successful retry."
            )
        )


class OrderFillBody(AlertEmailBody):
    def build(self, **ctx: Any) -> str:
        tag = ctx["tag"]
        symbol = ctx["symbol"]
        signal = ctx["signal"]
        strategy = ctx["strategy"]
        risk_decision = ctx["risk_decision"]
        filled_qty = ctx["filled_qty"]
        filled_px = ctx["filled_px"]
        client_order_id = ctx["client_order_id"]
        env = ctx["env"]
        state = ctx["state"]
        run_id = ctx["run_id"]
        return (
            f"{tag}: {symbol} {signal.side.upper()}\n\n"
            f"Strategy: {type(strategy).__name__}\n"
            f"Signal reason: {signal.reason}\n"
            f"Approved notional: ${risk_decision.approved_notional:,.2f}\n"
            + (
                f"Filled qty: {filled_qty}\nFilled avg price: ${float(filled_px):.4f}\n"
                if filled_qty is not None and filled_px is not None else
                "Fill not yet confirmed — status will be reconciled on a later tick.\n"
            )
            + f"Order id: {client_order_id}\n"
            f"Environment: {env}\n"
            f"Account equity: ${state.equity:,.2f}\n"
            f"Run id: {run_id}"
        )


class CspOpenBody(AlertEmailBody):
    def build(self, **ctx: Any) -> str:
        contract = ctx["contract"]
        symbol = ctx["symbol"]
        collateral = ctx["collateral"]
        strategy_tag = ctx["strategy_tag"]
        config = ctx["config"]
        client_order_id = ctx["client_order_id"]
        broker_order_id = ctx["broker_order_id"]
        run_id = ctx["run_id"]
        return (
            f"Cash-secured put opened: {contract.symbol}\n\n"
            f"Underlying: {symbol}\n"
            f"Strike: ${contract.strike:.2f}\n"
            f"Expiry: {contract.expiry.isoformat()}\n"
            f"Collateral locked: ${collateral:,.2f}\n"
            f"Strategy: {strategy_tag}\n"
            f"Environment: {'paper' if config.alpaca_options_paper else 'LIVE'}\n"
            f"Order id: {client_order_id}\n"
            f"Broker order id: {broker_order_id or 'unknown'}\n"
            f"Run id: {run_id}"
        )


_BUILDERS: dict[str, type[AlertEmailBody]] = {
    "daily_loss_breaker": DailyLossBreakerBody,
    "live_quote_failure": LiveQuoteFailureBody,
    "broker_stop_failed": BrokerStopFailedBody,
    "order_fill": OrderFillBody,
    "csp_open": CspOpenBody,
}


def get_email_body_builder(kind: str) -> AlertEmailBody:
    """Factory: returns the email-body-building strategy for the given alert kind."""
    try:
        return _BUILDERS[kind]()
    except KeyError:
        raise ValueError(f"unknown alert kind: {kind!r}") from None
