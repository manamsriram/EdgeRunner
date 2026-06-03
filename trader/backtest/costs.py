"""Transaction cost model: commission + slippage.

Without costs a backtest is fiction. The same model is intended to be reused by the
paper-sim fill path later, so backtest and paper agree on what a fill really costs.

Slippage is charged adversely: a buy fills slightly above the reference price, a sell
slightly below — never in your favour.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    commission_per_trade: float = 0.0   # flat $ per fill (Alpaca equities are $0)
    slippage_bps: float = 5.0           # adverse slippage in basis points of price

    def fill_price(self, reference_price: float, side: str) -> float:
        """Reference price (e.g. next bar's open) adjusted for adverse slippage."""
        slip = reference_price * (self.slippage_bps / 10_000.0)
        if side == "buy":
            return reference_price + slip
        if side == "sell":
            return reference_price - slip
        return reference_price

    def commission(self, notional: float) -> float:
        """Commission for a fill of the given notional value."""
        return self.commission_per_trade
