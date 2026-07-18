"""Dynamic stock universe via Alpaca's screener endpoints.

Replaces the static ALLOWED_SYMBOLS allowlist with a daily-refreshed list of
high-activity stocks: most-active by volume + top gainers + top losers.
Strategies are rebuilt against this list at each market open.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trader.config import Config

logger = logging.getLogger(__name__)

_MIN_PRICE = 1.0  # filter penny stocks from movers (price available on Mover, not ActiveStock)


def fetch_dynamic_universe(config: "Config", top_n: int = 100) -> list[str]:
    """Return up to `top_n` equity symbols screened from Alpaca's daily activity data.

    Sources (merged, deduplicated):
    - Most-active stocks by volume (top_n // 2 picks)
    - Top gainers by % change (top_n // 4 picks, price >= $1)
    - Top losers by % change (top_n // 4 picks, price >= $1)

    Crypto pairs (containing '/') are always excluded — they have a separate pipeline.
    Raises on API failure so the scheduler can fall back to the previous universe.
    """
    from alpaca.data.enums import MostActivesBy
    from alpaca.data.historical.screener import ScreenerClient
    from alpaca.data.requests import MarketMoversRequest, MostActivesRequest

    config.require_alpaca_credentials()
    client = ScreenerClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
    )

    n_actives = max(top_n // 2, 1)
    n_movers = max(top_n // 4, 1)

    actives_resp = client.get_most_actives(MostActivesRequest(top=n_actives, by=MostActivesBy.VOLUME))
    movers_resp = client.get_market_movers(MarketMoversRequest(top=n_movers))

    active_symbols = [s.symbol for s in actives_resp.most_actives]
    gainer_symbols = [
        s.symbol for s in movers_resp.gainers if s.price >= _MIN_PRICE
    ]
    loser_symbols = [
        s.symbol for s in movers_resp.losers if s.price >= _MIN_PRICE
    ]

    from trader.risk.gate import is_leveraged_etf_symbol

    merged: list[str] = list(
        dict.fromkeys(active_symbols + gainer_symbols + loser_symbols)
    )
    # Leveraged/inverse ETPs decay and reverse-split constantly, which the risk gate
    # hard-blocks on buy anyway (see is_leveraged_etf_symbol) — filtered here too so
    # they never occupy a universe slot or generate a signal that just gets rejected.
    equities = [
        s for s in merged if "/" not in s and not is_leveraged_etf_symbol(s)
    ][:top_n]

    logger.info(
        "screener: %d most-active + %d gainers + %d losers → %d unique equities",
        len(active_symbols),
        len(gainer_symbols),
        len(loser_symbols),
        len(equities),
    )
    return equities
