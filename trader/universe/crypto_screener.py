"""Dynamic crypto universe via Alpaca's daily-bars endpoint.

Ranks a fixed candidate pool of Alpaca-supported crypto pairs by daily dollar
volume (close x volume of the most recent daily bar), returns the top-N most
active. One API call for all candidates (batch request).

Stale pairs are excluded: Alpaca keeps returning "latest" bars for delisted
pairs (e.g. MATIC/USD's last bar is from 2023), so any bar older than
_MAX_BAR_AGE is treated as dead and dropped from the universe.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trader.config import Config

logger = logging.getLogger(__name__)

# A pair whose most recent daily bar is older than this is considered delisted
# or halted on Alpaca and excluded from the screened universe.
_MAX_BAR_AGE = timedelta(hours=48)

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


def rank_candidates(
    latest_bars: dict,
    *,
    top_n: int,
    now: datetime,
    max_age: timedelta = _MAX_BAR_AGE,
) -> list[str]:
    """Rank pairs by dollar volume (close x volume) of their latest daily bar.

    Excludes pairs whose bar is older than `max_age` (delisted/halted) and pairs
    with zero dollar volume (no trading activity on Alpaca).
    """
    scored = []
    for sym, bar in latest_bars.items():
        if bar is None or now - bar.timestamp > max_age:
            continue
        dollar_volume = float(bar.close) * float(bar.volume)
        if dollar_volume <= 0.0:
            continue
        scored.append((sym, dollar_volume))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return [sym for sym, _ in scored[:top_n]]


def fetch_dynamic_crypto_universe(config: "Config", top_n: int = 10) -> list[str]:
    """Return up to `top_n` crypto pairs ranked by latest daily dollar volume.

    Uses Alpaca's CryptoBarsRequest with a daily timeframe — one API call for
    the entire candidate pool. Pairs with no recent bar data (delisted, halted,
    or not supported on this account tier) are silently skipped.

    Raises on API failure so the scheduler can fall back to the previous universe.
    """
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    config.require_alpaca_credentials()
    client = CryptoHistoricalDataClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
    )

    now = datetime.now(timezone.utc)
    request = CryptoBarsRequest(
        symbol_or_symbols=list(CRYPTO_CANDIDATE_UNIVERSE),
        timeframe=TimeFrame.Day,
        start=now - _MAX_BAR_AGE - timedelta(days=1),
    )
    bars = client.get_crypto_bars(request)

    latest_bars = {
        sym: bar_list[-1]
        for sym, bar_list in bars.data.items()
        if bar_list
    }
    result = rank_candidates(latest_bars, top_n=top_n, now=now)
    logger.info(
        "crypto screener: ranked %d/%d candidates by dollar volume → top %d: %s",
        len(latest_bars),
        len(CRYPTO_CANDIDATE_UNIVERSE),
        len(result),
        result,
    )
    return result
