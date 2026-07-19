"""Dynamic universe screener: leveraged/inverse ETF filtering (symbol + name)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from trader.config import Config
from trader.universe.screener import fetch_dynamic_universe


def _config() -> Config:
    return Config(
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        alpaca_paper=True,
        autonomy="manual",
        openai_api_key=None,
        anthropic_api_key=None,
        portfolio_db_path=":memory:",
        kill_switch_path="kill_switch.flag",
    )


def _screener_resp(active_symbols, gainer_symbols, loser_symbols):
    return SimpleNamespace(
        most_actives=[SimpleNamespace(symbol=s) for s in active_symbols],
    ), SimpleNamespace(
        gainers=[SimpleNamespace(symbol=s, price=10.0) for s in gainer_symbols],
        losers=[SimpleNamespace(symbol=s, price=10.0) for s in loser_symbols],
    )


def test_fetch_dynamic_universe_filters_leveraged_etf_by_name():
    actives_resp, movers_resp = _screener_resp(["AAPL", "TSLL", "MSFT"], [], [])
    assets = [
        SimpleNamespace(symbol="AAPL", name="Apple Inc"),
        SimpleNamespace(symbol="TSLL", name="Direxion Daily TSLA Bull 2X Shares"),
        SimpleNamespace(symbol="MSFT", name="Microsoft Corp"),
    ]

    with patch("alpaca.data.historical.screener.ScreenerClient") as mock_screener_cls, \
         patch("alpaca.trading.client.TradingClient") as mock_trading_cls:
        mock_screener_cls.return_value.get_most_actives.return_value = actives_resp
        mock_screener_cls.return_value.get_market_movers.return_value = movers_resp
        mock_trading_cls.return_value.get_all_assets.return_value = assets

        result = fetch_dynamic_universe(_config(), top_n=10)

    assert result == ["AAPL", "MSFT"]  # TSLL dropped by name-based filter, not symbol list


def test_fetch_dynamic_universe_falls_back_when_asset_fetch_fails():
    actives_resp, movers_resp = _screener_resp(["AAPL"], [], [])

    with patch("alpaca.data.historical.screener.ScreenerClient") as mock_screener_cls, \
         patch("alpaca.trading.client.TradingClient") as mock_trading_cls:
        mock_screener_cls.return_value.get_most_actives.return_value = actives_resp
        mock_screener_cls.return_value.get_market_movers.return_value = movers_resp
        mock_trading_cls.return_value.get_all_assets.side_effect = RuntimeError("boom")

        result = fetch_dynamic_universe(_config(), top_n=10)

    assert result == ["AAPL"]  # asset fetch failure degrades to symbol-only filtering, not a crash
