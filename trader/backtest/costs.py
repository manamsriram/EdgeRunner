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
    taker_fee_bps: float = 0.0          # percentage fee per fill, in bps of notional
    # ↳ 0 for Alpaca equities/paper; crypto backtests should set this to the venue's
    #   real taker fee (Alpaca crypto ≈ 15–25 bps) so strategy selection isn't made on
    #   fee-free numbers that don't survive live trading.

    def fill_price(self, reference_price: float, side: str) -> float:
        """Reference price (e.g. next bar's open) adjusted for adverse slippage."""
        slip = reference_price * (self.slippage_bps / 10_000.0)
        if side == "buy":
            return reference_price + slip
        if side == "sell":
            return reference_price - slip
        raise ValueError(f"invalid side: {side!r}")

    def commission(self, notional: float) -> float:
        """Total cost for a fill: the flat per-trade commission plus the percentage
        taker fee on the notional (0 unless configured, e.g. crypto)."""
        return self.commission_per_trade + notional * (self.taker_fee_bps / 10_000.0)
