"""DipRecovery disaster-stop wiring: widened but not absent.

Guards the fix for the contradiction where DipRecovery was software-stop-exempt yet
still got an 8% broker GTC stop on every buy.
"""
from __future__ import annotations

from trader.pipeline import _stop_multiplier_for_owner
from trader.strategy.base import Strategy
from trader.strategy.dip_recovery import DipRecovery
from trader.strategy.supertrend import SuperTrend


def test_dip_recovery_widens_stop():
    assert DipRecovery.stop_loss_multiplier == 2.0


def test_default_strategy_multiplier_is_one():
    assert Strategy.stop_loss_multiplier == 1.0
    assert SuperTrend.stop_loss_multiplier == 1.0


def test_owner_lookup_resolves_dip_recovery():
    assert _stop_multiplier_for_owner("DipRecovery") == 2.0


def test_owner_lookup_defaults_for_unknown_or_none():
    assert _stop_multiplier_for_owner("SuperTrend") == 1.0
    assert _stop_multiplier_for_owner(None) == 1.0
    assert _stop_multiplier_for_owner("NoSuchStrategy") == 1.0


def test_dip_recovery_still_has_a_catastrophe_stop():
    # 2.0 * 8% = 16%: widened, but a real stop — not exempt/infinite.
    assert 0.0 < DipRecovery.stop_loss_multiplier * 0.08 < 0.25
