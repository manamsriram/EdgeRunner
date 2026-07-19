import pandas as pd
from trader.overlay.market_stats import compute_bar_stats


def _bars(closes):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"close": closes}, index=idx)


def test_compute_bar_stats_empty_returns_empty_dict():
    assert compute_bar_stats(pd.DataFrame()) == {}


def test_compute_bar_stats_insufficient_bars_returns_empty_dict():
    assert compute_bar_stats(_bars([100.0])) == {}


def test_compute_bar_stats_basic_shape():
    closes = [100.0 + i for i in range(30)]
    stats = compute_bar_stats(_bars(closes))
    assert stats["last_close"] == 129.0
    assert stats["n_days"] == 30
    assert stats["lookback_20"] == 20
    assert stats["lookback_10"] == 10
    assert isinstance(stats["pct_20d"], float)
    assert isinstance(stats["vol_10d_annualized"], float)
