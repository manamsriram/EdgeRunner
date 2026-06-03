"""Execution: the Alpaca broker adapter on the order path.

The broker is the source of truth for positions — `reconcile()` pulls live account
state from Alpaca into the shape the risk gate consumes, and fails closed (stale) on any
error. Orders are idempotent via a deterministic `client_order_id`, so a crash/retry can
never double-fire. Config.require_alpaca() currently restricts execution to paper mode;
enabling live trading requires changing Config.require_alpaca() or the configuration flow.
"""
from trader.execution.broker import AlpacaBroker, client_order_id_for

__all__ = ["AlpacaBroker", "client_order_id_for"]
