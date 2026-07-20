import pandas as pd
from trader.ml_overlay.features import build_feature_vector
from trader.strategy.base import Signal


def _bars(closes):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"close": closes}, index=idx)


def test_build_feature_vector_basic_shape():
    signal = Signal("AAPL", "buy", 0.8, "dip recovery entry")
    bars = _bars([100.0 + i for i in range(30)])
    news = {"EARNINGS": [{"headline": "beats estimate", "datetime": "2026-01-29T09:00:00"}]}
    fundamentals = {"pe_ttm": 22.5, "gross_margin_ttm": 40.0}
    recent_outcomes = [
        {"closed_at": "2026-01-20T00:00:00", "pnl_pct": 0.05, "exit_reason": "signal-exit"},
        {"closed_at": "2026-01-10T00:00:00", "pnl_pct": -0.02, "exit_reason": "stop-loss"},
    ]
    features = build_feature_vector(
        signal, bars, news_categories=news, sentiment=None,
        fundamentals=fundamentals, recent_outcomes=recent_outcomes, regime="normal",
    )
    assert features["signal_strength"] == 0.8
    assert features["news_earnings_count"] == 1.0
    assert features["news_regulatory_count"] == 0.0
    assert features["sentiment_bullish_ratio"] == 0.0  # default when sentiment is None
    assert features["fund_pe_ttm"] == 22.5
    assert features["fund_gross_margin_ttm"] == 40.0
    assert features["fund_ev_fcf_ttm"] == 0.0  # default when Finnhub didn't return it
    assert features["last_trade_pnl_pct"] == 0.05  # most recent outcome
    assert features["win_rate_last_3"] == 0.5
    assert features["days_since_last_trade"] >= 0.0
    assert features["regime_calm"] == 0.0
    assert features["regime_normal"] == 1.0
    assert features["regime_stressed"] == 0.0


def test_build_feature_vector_no_history_defaults():
    signal = Signal("XYZ", "buy", 0.5, "entry")
    features = build_feature_vector(
        signal, pd.DataFrame(), news_categories={}, sentiment=None,
        fundamentals={}, recent_outcomes=[], regime="normal",
    )
    assert features["last_close"] == 0.0
    assert features["last_trade_pnl_pct"] == 0.0
    assert features["win_rate_last_3"] == 0.0
    assert features["days_since_last_trade"] == -1.0  # sentinel: no prior trade
