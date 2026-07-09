"""Performance metrics route — live paper trading scorecard."""
from __future__ import annotations

import asyncio
import logging
import math
import time

from fastapi import APIRouter, HTTPException

from api.deps import get_broker, get_config, get_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/performance", tags=["performance"])

_CACHE_TTL = 300  # 5 minutes
_cache: dict = {}


def _serialize(result) -> dict:
    """Convert LiveMetrics to a JSON-safe dict. float('inf') → null."""
    def _safe(v):
        if v is None:
            return None
        if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
            return None
        return v

    return {
        "days_active": result.days_active,
        "trade_count": result.trade_count,
        "sharpe": _safe(result.sharpe),
        "max_drawdown": _safe(result.max_drawdown),
        "win_rate": _safe(result.win_rate),
        "profit_factor": _safe(result.profit_factor),
        "total_return": _safe(result.total_return),
        "benchmark_spy_return": _safe(result.benchmark_spy_return),
        "benchmark_btc_return": _safe(result.benchmark_btc_return),
        "verdict": result.verdict,
        "failing_checks": result.failing_checks,
        "strategy_signals": result.strategy_signals,
    }


def _compute_sync(config, broker, repo) -> dict:
    from trader.performance.metrics import compute_live_metrics
    result = compute_live_metrics(config, broker, repo)
    return _serialize(result)


_EMPTY_RESPONSE = {
    "days_active": 0, "trade_count": 0, "sharpe": 0.0,
    "max_drawdown": 0.0, "win_rate": 0.0, "profit_factor": None,
    "total_return": 0.0, "benchmark_spy_return": None,
    "benchmark_btc_return": None, "verdict": "INSUFFICIENT_DATA",
    "failing_checks": [], "strategy_signals": {},
}


@router.get("")
async def get_performance():
    now = time.monotonic()
    if _cache.get("computed_at", 0) + _CACHE_TTL > now:
        return _cache["result"]

    try:
        config = get_config()
        broker = get_broker()
        repo = get_repo()

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _compute_sync, config, broker, repo)

        _cache["result"] = data
        _cache["computed_at"] = now
        return data

    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception:
        logger.exception("performance compute failed")
        return _EMPTY_RESPONSE
