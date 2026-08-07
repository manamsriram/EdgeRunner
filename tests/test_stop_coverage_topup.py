"""Guards the SNAP incident: a buy fill under-covered by its broker stop (Alpaca's
insufficient-qty retry protects only what's "available" at placement time) must get
topped up on a later tick, not stay short forever.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from trader.config import load_config
from trader.pipeline import _top_up_stop_coverage
from trader.risk.gate import AccountState


def _state(**overrides) -> AccountState:
    base = dict(
        equity=10_000.0, positions={}, open_order_symbols=frozenset(),
        trades_today=0, daily_pnl_pct=0.0,
    )
    base.update(overrides)
    return AccountState(**base)


def test_tops_up_shortfall_when_stop_undercovers_position():
    broker = MagicMock()
    broker.get_open_stop_qty.return_value = 567.0
    repo = MagicMock()
    repo.get_highest_buy_price.return_value = 5.73
    state = _state(
        positions={"SNAP": 606.823734729},
        position_owners={("SNAP", "daily"): "DipRecovery"},
    )

    _top_up_stop_coverage(broker, repo, state, load_config())

    broker.place_stop_order.assert_called_once()
    kwargs = broker.place_stop_order.call_args.kwargs
    assert kwargs["symbol"] == "SNAP"
    assert kwargs["qty"] == 39  # int(606.82... - 567)
    # DipRecovery's 1.5x multiplier: 5.73 * (1 - 0.08*1.5) = 5.0424
    assert round(kwargs["stop_price"], 4) == round(5.73 * (1 - 0.12), 4)


def test_no_action_when_fully_covered():
    broker = MagicMock()
    broker.get_open_stop_qty.return_value = 606.823734729
    repo = MagicMock()
    state = _state(positions={"SNAP": 606.823734729})

    _top_up_stop_coverage(broker, repo, state, load_config())

    broker.place_stop_order.assert_not_called()


def test_skips_crypto_and_dust_positions():
    broker = MagicMock()
    repo = MagicMock()
    state = _state(positions={"BAT/USD": 500.0, "AAPL": 0.5})

    _top_up_stop_coverage(broker, repo, state, load_config())

    broker.get_open_stop_qty.assert_not_called()
    broker.place_stop_order.assert_not_called()
