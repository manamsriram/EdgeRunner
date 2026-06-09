"""Strategy contract + concrete quant signals."""
from trader.strategy.base import Signal, Strategy
from trader.strategy.donchian_breakout import DonchianBreakout
from trader.strategy.equity_reversion import EquityBollingerReversion
from trader.strategy.ma_crossover import MACrossover
from trader.strategy.smash_day import SmashDayB
from trader.strategy.supertrend import SuperTrend

__all__ = [
    "DonchianBreakout",
    "EquityBollingerReversion",
    "MACrossover",
    "Signal",
    "SmashDayB",
    "Strategy",
    "SuperTrend",
]
