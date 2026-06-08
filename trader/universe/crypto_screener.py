"""Dynamic crypto universe via Alpaca's latest-bar endpoint.

Ranks a fixed candidate pool of Alpaca-supported crypto pairs by 24h volume,
returns the top-N most active. One API call for all candidates (batch request).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trader.config import Config

logger = logging.getLogger(__name__)

# All crypto pairs currently tradeable on Alpaca (USD-quoted only).
# Updated as Alpaca adds listings; symbols not in this list are never screened.
CRYPTO_CANDIDATE_UNIVERSE: tuple[str, ...] = (
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "DOGE/USD",
    "AVAX/USD",
    "LINK/USD",
    "UNI/USD",
    "AAVE/USD",
    "XRP/USD",
    "LTC/USD",
    "BCH/USD",
    "DOT/USD",
    "MATIC/USD",
    "ADA/USD",
    "ALGO/USD",
    "ATOM/USD",
    "CRV/USD",
    "GRT/USD",
    "MKR/USD",
    "BAT/USD",
    "SUSHI/USD",
    "YFI/USD",
    "COMP/USD",
    "FIL/USD",
    "SHIB/USD",
)


def fetch_dynamic_crypto_universe(config: "Config", top_n: int = 10) -> list[str]:
    """Return up to `top_n` crypto pairs ranked by most recent 24h volume.

    Uses Alpaca's CryptoLatestBarRequest — one API call for the entire candidate
    pool. Pairs with no bar data (exchange gap, Alpaca not supporting the pair
    on this account tier) are silently skipped.

    Raises on API failure so the scheduler can fall back to the previous universe.
    """
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoLatestBarRequest

    config.require_alpaca_credentials()
    client = CryptoHistoricalDataClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
    )

    request = CryptoLatestBarRequest(symbol_or_symbols=list(CRYPTO_CANDIDATE_UNIVERSE))
    latest = client.get_crypto_latest_bar(request)

    # latest is dict[str, Bar]; rank by volume descending.
    ranked = sorted(
        ((sym, bar) for sym, bar in latest.items() if bar is not None),
        key=lambda kv: kv[1].volume,
        reverse=True,
    )

    result = [sym for sym, _ in ranked[:top_n]]
    logger.info(
        "crypto screener: ranked %d/%d candidates by volume → top %d: %s",
        len(ranked),
        len(CRYPTO_CANDIDATE_UNIVERSE),
        len(result),
        result,
    )
    return result
