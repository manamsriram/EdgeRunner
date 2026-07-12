"""Spike: evaluate londonstrategicedge.com (LSE) daily OHLCV quality vs Alpaca.

One-shot data-quality probe — NOT a data-source integration. Decides whether LSE
history is trustworthy enough to feed the backtest harness later. Backtest-only
regardless; never live trading.

Compares, per symbol, LSE daily candles against the existing Alpaca baseline
(`trader.data.alpaca_bars.get_daily_bars`, which is split+dividend adjusted via
Adjustment.ALL). Four axes:
  1. Coverage           — row counts, date span, gaps on the overlap window.
  2. Split-adjustment   — DECISIVE. Day-over-day close ratio near known splits.
                          Adjusted -> ~1.0; RAW -> ~4x/5x cliff. RAW = unusable.
  3. Accuracy           — daily-returns correlation vs Alpaca (scale-invariant,
                          so a different adjustment reference date doesn't false-FAIL).
  4. Deep-history sanity— pre-2016 range has no Alpaca baseline; internal OHLC
                          integrity checks only.

Exits non-zero if the split-adjustment check FAILs for any symbol, so the
go/no-go is unmissable.

Usage:
    echo "LSE_API_KEY=lse_live_..." >> .env
    python scripts/eval_lse_data.py
    python scripts/eval_lse_data.py --symbols AAPL,MSFT
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.data.alpaca_bars import BAR_COLUMNS, get_daily_bars

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("eval_lse")

LSE_BASE_URL = "https://api.londonstrategicedge.com/vault"
LSE_PAGE_CAP = 5000  # docs: "at most one page of rows, currently 5,000"

# Known splits for the decisive adjustment test: (ex_date, ratio). A correctly
# back-adjusted series shows a day-over-day close ratio ~= 1.0 across ex_date;
# a RAW series shows a ratio near `ratio` (a cliff).
KNOWN_SPLITS: dict[str, tuple[str, float]] = {
    "AAPL": ("2020-08-31", 4.0),
    "TSLA": ("2020-08-31", 5.0),
}

# Deliberate mix: splits (AAPL/TSLA) + real EdgeRunner-universe clean name (MSFT)
# + deep-history name past Alpaca's ~2016 floor (GE).
DEFAULT_SYMBOLS = ["AAPL", "TSLA", "MSFT", "GE"]

# Thresholds
RATIO_SPLIT_TOL = 0.15      # |ratio-1| above this near a split == RAW == FAIL
RETURNS_CORR_MIN = 0.99     # below this on the overlap == accuracy SUSPECT
RETURNS_DIFF_MAX = 0.01     # any single-day |returns diff| above this == SUSPECT


# ---- LSE fetch ----

def fetch_lse_candles(symbol: str, api_key: str, start: str, end: str,
                      timeframe: str = "1d") -> pd.DataFrame:
    """Fetch daily candles for one symbol, paginating past the 5,000-row page cap.

    LSE returns at most LSE_PAGE_CAP rows per call; 20+ years of daily bars exceeds
    that, so we page forward by advancing `start` to just past the last bar's date
    until a short page comes back. Returns the standard OHLCV frame.
    """
    session = requests.Session()
    session.headers["x-api-key"] = api_key

    rows: list[dict] = []
    cursor = start
    while True:
        resp = session.get(
            f"{LSE_BASE_URL}/candles",
            params={
                "symbol": symbol,
                "timeframe": timeframe,
                "start": cursor,
                "end": end,
                "order": "asc",
                "limit": LSE_PAGE_CAP,
            },
            timeout=60,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        rows.extend(page)
        if len(page) < LSE_PAGE_CAP:
            break
        # Advance cursor to the day after the last bar to avoid re-fetching it.
        last_ts = pd.to_datetime(page[-1]["ts"]).normalize()
        cursor = (last_ts + timedelta(days=1)).strftime("%Y-%m-%d")

    return _to_frame_lse(rows, symbol)


def _to_frame_lse(rows: list[dict], symbol: str) -> pd.DataFrame:
    """Normalise LSE candle JSON into the same shape as trader.data.alpaca_bars:
    tz-naive daily DatetimeIndex, columns [open, high, low, close, volume]."""
    if not rows:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["ts"]).dt.tz_localize(None).dt.normalize()
    df.index.name = "date"
    df = df[BAR_COLUMNS].astype(float)
    # Dedup any boundary overlap from pagination; keep chronological order.
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


# ---- comparison axes ----

def check_split_adjustment(lse: pd.DataFrame, symbol: str) -> tuple[str, str]:
    """DECISIVE. Ratio-based: is the LSE close smooth across a known split?"""
    split = KNOWN_SPLITS.get(symbol)
    if split is None:
        return "N/A", "no known split in test window"
    ex_date, ratio = split
    ex = pd.Timestamp(ex_date)
    closes = lse["close"]
    before = closes[closes.index < ex]
    after = closes[closes.index >= ex]
    if before.empty or after.empty:
        return "N/A", f"no bars bracketing {ex_date}"
    jump = before.iloc[-1] / after.iloc[0]  # >1 pre-split if RAW (price was higher)
    if abs(jump - 1.0) <= RATIO_SPLIT_TOL:
        return "PASS", f"close ratio across {ex_date} = {jump:.2f} (adjusted)"
    if abs(jump - ratio) <= ratio * RATIO_SPLIT_TOL:
        return "FAIL", f"close ratio across {ex_date} = {jump:.2f} ~= {ratio:.0f}x (RAW, unadjusted)"
    return "SUSPECT", f"close ratio across {ex_date} = {jump:.2f} (neither ~1 nor ~{ratio:.0f}x)"


def check_accuracy(lse: pd.DataFrame, alpaca: pd.DataFrame) -> tuple[str, str]:
    """Scale-invariant: daily-returns correlation on the date intersection."""
    common = lse.index.intersection(alpaca.index)
    if len(common) < 30:
        return "N/A", f"only {len(common)} overlapping days (<30), no Alpaca baseline"
    lr = lse.loc[common, "close"].pct_change().dropna()
    ar = alpaca.loc[common, "close"].pct_change().dropna()
    common_r = lr.index.intersection(ar.index)
    corr = lr.loc[common_r].corr(ar.loc[common_r])
    max_diff = (lr.loc[common_r] - ar.loc[common_r]).abs().max()
    detail = f"returns corr={corr:.4f}, max|diff|={max_diff:.4f} over {len(common_r)} days"
    if corr >= RETURNS_CORR_MIN and max_diff <= RETURNS_DIFF_MAX:
        return "PASS", detail
    return "SUSPECT", detail


def check_coverage(lse: pd.DataFrame, alpaca: pd.DataFrame) -> tuple[str, str]:
    if lse.empty:
        return "FAIL", "LSE returned no rows"
    span = f"{lse.index[0].date()}..{lse.index[-1].date()} ({len(lse)} rows)"
    if alpaca.empty:
        return "PASS", f"LSE {span}; no Alpaca baseline to diff"
    common = lse.index.intersection(alpaca.index)
    only_alpaca = len(alpaca.index.difference(lse.index[lse.index >= alpaca.index[0]]))
    return "PASS", f"LSE {span}; {len(common)} shared days; {only_alpaca} Alpaca days missing from LSE"


def check_sanity(df: pd.DataFrame) -> tuple[str, str]:
    """Internal OHLC integrity — the only check available for pre-2016 history."""
    if df.empty:
        return "FAIL", "empty frame"
    problems = []
    if not df.index.is_monotonic_increasing:
        problems.append("dates not monotonic")
    if df.index.has_duplicates:
        problems.append("duplicate dates")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        problems.append("non-positive OHLC")
    if (df["volume"] < 0).any():
        problems.append("negative volume")
    hi = df[["open", "close", "high"]].max(axis=1)
    lo = df[["open", "close", "low"]].min(axis=1)
    if (df["high"] < hi - 1e-6).any() or (df["low"] > lo + 1e-6).any():
        problems.append("high/low do not bound open/close")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", f"{len(df)} rows OHLC-consistent"


# ---- driver ----

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="comma-separated override", default=None)
    parser.add_argument("--start", default="2003-01-01", help="LSE fetch start (ISO)")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("LSE_API_KEY")
    if not api_key:
        logger.error("LSE_API_KEY not set. Add it to .env (see script docstring).")
        return 2

    symbols = ([s.strip().upper() for s in args.symbols.split(",")]
               if args.symbols else DEFAULT_SYMBOLS)
    end = datetime.now().strftime("%Y-%m-%d")
    start_dt = datetime.strptime(args.start, "%Y-%m-%d")

    split_failed = False
    logger.info("\n%-6s %-10s %-8s %s", "SYMBOL", "AXIS", "VERDICT", "DETAIL")
    logger.info("%s", "-" * 78)

    for symbol in symbols:
        try:
            lse = fetch_lse_candles(symbol, api_key, args.start, end)
        except requests.HTTPError as e:
            logger.error("%-6s FETCH      FAIL     %s", symbol, e)
            continue
        except Exception as e:  # noqa: BLE001 — spike, surface anything
            logger.error("%-6s FETCH      FAIL     %s", symbol, e)
            continue

        # Alpaca baseline only where it has history (~2016+); harmless if empty.
        try:
            alpaca = get_daily_bars(symbol, start=start_dt, end=datetime.now())
        except Exception as e:  # noqa: BLE001
            logger.warning("%-6s ALPACA     N/A      baseline fetch failed: %s", symbol, e)
            alpaca = pd.DataFrame(columns=BAR_COLUMNS)

        for axis, (verdict, detail) in [
            ("coverage", check_coverage(lse, alpaca)),
            ("split-adj", check_split_adjustment(lse, symbol)),
            ("accuracy", check_accuracy(lse, alpaca)),
            ("sanity", check_sanity(lse)),
        ]:
            logger.info("%-6s %-10s %-8s %s", symbol, axis, verdict, detail)
            if axis == "split-adj" and verdict == "FAIL":
                split_failed = True
        logger.info("%s", "-" * 78)

    if split_failed:
        logger.error("\n*** FAIL: LSE candles are NOT split-adjusted -> unusable for this "
                     "backtest harness (RAW splits poison drawdown/lookback strategies). ***")
        return 1
    logger.info("\nSplit-adjustment PASS. Review accuracy/coverage above before any "
                "integration follow-up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
