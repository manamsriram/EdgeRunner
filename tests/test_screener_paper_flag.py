"""The screener's TradingClient must inherit config.alpaca_paper.

alpaca-py defaults `paper=True`. Omitting it sends live credentials to
paper-api.alpaca.markets, which 401s — caught by the broad `except` in
screener.py, so the only symptom is the name-based leveraged-ETF filter
silently falling back to the hand-maintained symbol list. That degradation
happens on the live account, which is the one place it matters.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trader.universe.screener import fetch_dynamic_universe


@pytest.mark.parametrize("alpaca_paper", [True, False])
def test_trading_client_gets_paper_flag_from_config(alpaca_paper):
    config = MagicMock()
    config.alpaca_api_key = "k"
    config.alpaca_secret_key = "s"
    config.alpaca_paper = alpaca_paper

    actives = MagicMock(most_actives=[])
    movers = MagicMock(gainers=[], losers=[])

    # screener.py imports these inside the function, so patch them at the source.
    with patch("alpaca.trading.client.TradingClient") as tc, \
         patch("alpaca.data.historical.screener.ScreenerClient") as sc:
        tc.return_value.get_all_assets.return_value = []
        sc.return_value.get_most_actives.return_value = actives
        sc.return_value.get_market_movers.return_value = movers

        fetch_dynamic_universe(config, top_n=5)

    assert tc.called, "TradingClient was never constructed"
    assert tc.call_args.kwargs.get("paper") == alpaca_paper, (
        f"TradingClient must be built with paper={alpaca_paper}; got "
        f"{tc.call_args.kwargs.get('paper')!r}. Omitting it defaults to "
        "paper=True and 401s on a live account."
    )
