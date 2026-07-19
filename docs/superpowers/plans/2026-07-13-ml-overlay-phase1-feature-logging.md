# ML-Overlay Phase 1: Feature-Snapshot Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At every overlay decision, log the full numeric feature vector the LLM
overlay sees today (price/vol, news, sentiment, fundamentals, trade memory, regime)
to a new `decision_features` table, linked 1:1 to the resulting order when one is
placed, so a labeled dataset accumulates for later ML training (Phase 2 of
`~/.claude/plans/groovy-riding-goose.md`).

**Architecture:** A pure function `build_feature_vector(...) -> dict[str, float]`
in a new `trader/ml_overlay/` package computes the feature dict from objects the
pipeline already fetches (no new API calls when singletons + 60s cache are warm;
see Global Constraints above). `pipeline.py::_prepare_signal` logs a
`decision_features` row BEFORE `apply_overlay` so the feature vector captures
the strategy's view, not the LLM's post-overlay view. When an order is later
placed for that signal, `_execute_signal` (equity path) AND `_execute_csp_entry`
(Wheel/CSP path) back-fill `decision_features.order_id` on the same row — holds,
vetoes, and any un-linked rows keep `order_id = NULL` forever. A new nightly job
`trader/learning/link_outcomes.py` is scaffolded per the plan's spec but does no
real work yet, since no trade has both a `decision_features` row and a matched
`trade_outcomes` row until this ships and a full round-trip completes live.

**Tech Stack:** Python, pandas, psycopg2 (Postgres), sqlite3, Alembic, pytest.
No new third-party dependencies.

## Global Constraints

- Branch: `research/ml-overlay`. Never touch `main` in this phase.
- Bound external API call volume — `_log_decision_features` reuses the same
  Finnhub client singletons the LLM overlay uses, behind a 60-second in-process
  cache keyed by `symbol` (the news cache adds `finnhub_key` for completeness
  since the LLM overlay uses the same client) — see Task 7 cache layer for
  exact keys. Worst-case volume stays at 1 external call per `symbol` per
  minute for fundamentals, and at 1 per `(symbol, finnhub_key)` per minute for
  news, regardless of how many times the pipeline touches a symbol each tick.
  Note that both `basic_financials` and `recommendation_trends` share one
  fundamentals-cache entry per `symbol` — by construction, since `_log_decision_features`
  fetches and caches them atomically. This is the partial mitigation; the fully
  "no new calls" alternative (threading pre-fetched objects through
  `apply_overlay`'s signature) is deferred — flag in PR.
- `bars` reaching the feature builder is already truncated to `asof` by
  `Strategy.generate` (`trader/strategy/base.py:99-111`) — never pass an untruncated
  frame into `build_feature_vector`.
- Alembic migrations: one DDL statement per `op.execute()` call — multi-statement
  `op.execute` can silently skip statements on some psycopg2 versions (documented
  gotcha, see `migrations/versions/003_trade_outcomes.py`).
- Dual-backend: every new repository method needs an abstract signature in
  `trader/portfolio/repository.py`, and an implementation in both
  `postgres_repo.py` (JSONB column) and `sqlite_repo.py` (TEXT column,
  `json.dumps`/`json.loads` at the boundary — SQLite has no native JSONB).
- Order linking is via `orders.id -> decision_features.order_id`
  (`record_order` already returns `int` id in both backends) — NOT
  `(symbol, run_id)` matching, which is ambiguous (many decisions can occur
  between two fills).
- Run `rtk proxy venv/bin/python -m pytest` after every task; commit per task.
- The current migration head is `007` (`007_options_positions_unique_key.py`) —
  the plan file's original numbering (`005_decision_features.py`) is stale; this
  plan uses `008_decision_features.py`.

---

### Task 1: Shared market-stats module

**Files:**
- Create: `trader/overlay/market_stats.py`
- Modify: `trader/overlay/claude_overlay.py:138-154` (replace `_bars_context` body,
  keep the function as a thin formatter calling the new module)
- Test: `tests/test_market_stats.py`

**Interfaces:**
- Produces: `trader/overlay/market_stats.py::compute_bar_stats(bars: pd.DataFrame) -> dict`
  returning `{"last_close": float, "pct_20d": float, "vol_10d_annualized": float,
  "n_days": int, "lookback_20": int, "lookback_10": int}` — empty dict `{}` if
  `bars.empty or len(bars) < 2`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_market_stats.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk proxy venv/bin/python -m pytest tests/test_market_stats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.overlay.market_stats'`

- [ ] **Step 3: Write minimal implementation**

```python
# trader/overlay/market_stats.py
"""Shared price/vol statistics — one source of truth for the LLM overlay's
prompt context and the ML-overlay feature builder, so the two never drift.
"""
from __future__ import annotations

import pandas as pd


def compute_bar_stats(bars: pd.DataFrame) -> dict:
    """Derive price/vol stats from an already asof-truncated bars DataFrame.

    Returns {} if there isn't enough history (mirrors the LLM prompt's own
    "insufficient bar data" fallback).
    """
    if bars.empty or len(bars) < 2:
        return {}
    close = bars["close"]
    last_close = float(close.iloc[-1])
    lookback_20 = min(20, len(close) - 1)
    pct_20d = float((close.iloc[-1] / close.iloc[-(lookback_20 + 1)] - 1) * 100)
    lookback_10 = min(10, len(close) - 1)
    returns_10 = close.pct_change().dropna().iloc[-lookback_10:]
    vol_10d = float(returns_10.std() * (252 ** 0.5) * 100) if len(returns_10) > 1 else 0.0
    return {
        "last_close": last_close,
        "pct_20d": pct_20d,
        "vol_10d_annualized": vol_10d,
        "n_days": len(close),
        "lookback_20": lookback_20,
        "lookback_10": lookback_10,
    }
```

- [ ] **Step 4: Update `claude_overlay.py` to use it**

Replace `trader/overlay/claude_overlay.py:138-154`:

```python
from trader.overlay.market_stats import compute_bar_stats


def _bars_context(symbol: str, bars: pd.DataFrame) -> str:
    stats = compute_bar_stats(bars)
    if not stats:
        return f"Insufficient bar data for {symbol}."
    return (
        f"Market context ({symbol}, last {stats['n_days']} trading days):\n"
        f"- Last close: ${stats['last_close']:.2f}\n"
        f"- {stats['lookback_20']}-day price change: {stats['pct_20d']:+.1f}%\n"
        f"- {stats['lookback_10']}-day annualized volatility: {stats['vol_10d_annualized']:.1f}%"
    )
```

- [ ] **Step 5: Run tests to verify everything passes**

Run: `rtk proxy venv/bin/python -m pytest tests/test_market_stats.py tests/test_overlay.py -v`
Expected: PASS (existing `test_overlay.py` assertions on `_bars_context`'s output
string must still pass unchanged — this is a pure refactor).

- [ ] **Step 6: Commit**

```bash
git add trader/overlay/market_stats.py trader/overlay/claude_overlay.py tests/test_market_stats.py
git commit -m "refactor(overlay): extract bar stats into shared market_stats module"
```

---

### Task 2: Retain article datetime in news classification

**Files:**
- Modify: `trader/overlay/news_context.py` (`classify_news`, `fetch_news_finnhub`)
- Test: `tests/test_news_context.py` (extend existing or create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `classify_news(articles: list[dict]) -> dict[str, list[dict]]` — CHANGED
  signature: now takes `list[dict]` (each `{"headline": str, "datetime": str}`) instead
  of `list[str]`, returns `{CATEGORY: [{"headline": ..., "datetime": ...}, ...]}`.
  `format_classified_news(symbol, categories) -> str` updated to read `.["headline"]`
  off each dict — output text is unchanged (still headline-only), only the internal
  shape changes so the feature builder can read `datetime` too.
  New: `fetch_news_finnhub_raw(symbol: str, api_key: str) -> list[dict]` returning
  the classified-with-datetime structure, used by both `fetch_news_finnhub` (which
  formats it to text as before) and the Phase 1 feature builder.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_news_context.py (add to existing file, or create if none exists)
from trader.overlay.news_context import classify_news, format_classified_news


def test_classify_news_retains_datetime():
    articles = [
        {"headline": "Company beats earnings estimate", "datetime": "2026-07-10T09:00:00"},
        {"headline": "Random unrelated headline", "datetime": "2026-07-11T09:00:00"},
    ]
    result = classify_news(articles)
    assert "EARNINGS" in result
    assert result["EARNINGS"][0]["headline"] == "Company beats earnings estimate"
    assert result["EARNINGS"][0]["datetime"] == "2026-07-10T09:00:00"


def test_format_classified_news_unchanged_output_shape():
    articles = [{"headline": "Company beats earnings estimate", "datetime": "2026-07-10T09:00:00"}]
    categories = classify_news(articles)
    text = format_classified_news("AAPL", categories)
    assert "[EARNINGS] Company beats earnings estimate" in text
    assert "2026-07-10" not in text  # formatted text stays headline-only
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk proxy venv/bin/python -m pytest tests/test_news_context.py -v`
Expected: FAIL — `classify_news` currently expects `list[str]` and indexes headlines
as strings, so `result["EARNINGS"][0]["headline"]` raises `TypeError`.

- [ ] **Step 3: Write minimal implementation**

Replace in `trader/overlay/news_context.py`:

```python
def classify_news(articles: list[dict]) -> dict[str, list[dict]]:
    """Map each article to matching categories. Each article is {"headline": str,
    "datetime": str}. Returns {CATEGORY: [article, ...]}."""
    result: dict[str, list[dict]] = {}
    for article in articles:
        hl_lower = article["headline"].lower()
        for category, keywords in _NEWS_CATEGORIES.items():
            if any(kw in hl_lower for kw in keywords):
                result.setdefault(category, []).append(article)
    return result


def format_classified_news(symbol: str, categories: dict[str, list[dict]]) -> str:
    """Format classified news for LLM user message. Returns '' if no categories."""
    if not categories:
        return ""
    parts = [f"Recent news ({symbol}):"]
    for cat, articles in categories.items():
        for a in articles[:2]:  # max 2 per category
            parts.append(f"[{cat}] {a['headline']}")
    return "\n".join(parts)
```

Update `fetch_news_finnhub` (same file) to build `list[dict]` instead of
`list[str]` before calling `classify_news`:

```python
def fetch_news_finnhub(symbol: str, api_key: str) -> str:
    """Fetch and classify company news from Finnhub. Returns '' on any failure."""
    try:
        categories = _fetch_finnhub_articles_classified(symbol, api_key)
        if not categories:
            return ""
        return format_classified_news(symbol, categories)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("fetch_news_finnhub failed for %s: %s", symbol, exc)
        return ""


def _fetch_finnhub_articles_classified(symbol: str, api_key: str) -> dict[str, list[dict]]:
    """Fetch raw Finnhub articles (headline + datetime) and classify them.
    Returns {} on no articles. Shared by fetch_news_finnhub (LLM prompt) and
    the Phase 1 feature builder (trader/ml_overlay/features.py)."""
    from datetime import date, timedelta
    client = _get_finnhub_client(api_key)
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    raw_articles = client.company_news(symbol, from_date=week_ago, to_date=today, limit=8)
    articles = [
        {"headline": a["headline"], "datetime": a.get("datetime", "")}
        for a in raw_articles if a.get("headline")
    ]
    if not articles:
        return {}
    return classify_news(articles)
```

- [ ] **Step 4: Run tests to verify everything passes**

Run: `rtk proxy venv/bin/python -m pytest tests/test_news_context.py tests/test_overlay.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/overlay/news_context.py tests/test_news_context.py
git commit -m "feat(overlay): retain article datetime through news classification"
```

---

### Task 3: Parsed-float fundamentals helper

**Files:**
- Modify: `trader/overlay/fundamental_gate.py`
- Test: `tests/test_fundamental_gate.py` (extend existing)

**Interfaces:**
- Produces: `parse_fundamentals_finnhub(metrics: dict, recs: list[dict]) -> dict[str, float]`
  — pulls the same fields `fetch_fundamentals_finnhub` already formats into text, but
  returns floats: `{"pe_ttm": float, "ev_fcf_ttm": float, "gross_margin_ttm": float,
  "revenue_growth_yoy": float, "analyst_buy_count": float, "analyst_hold_count": float,
  "analyst_sell_count": float}`. Any field Finnhub didn't return is simply absent from
  the dict (not zero-filled) — the feature builder fills defaults, this function stays
  a pure "what did Finnhub actually give us" extractor.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_fundamental_gate.py
from trader.overlay.fundamental_gate import parse_fundamentals_finnhub


def test_parse_fundamentals_finnhub_extracts_floats():
    metrics = {
        "peBasicExclExtraTTM": 22.5,
        "currentEv/freeCashFlowTTM": 18.0,
        "grossMarginTTM": 42.3,
        "revenueGrowthTTMYoy": 12.1,
    }
    recs = [{"buy": 10, "hold": 4, "sell": 1, "period": "2026-06"}]
    parsed = parse_fundamentals_finnhub(metrics, recs)
    assert parsed["pe_ttm"] == 22.5
    assert parsed["ev_fcf_ttm"] == 18.0
    assert parsed["gross_margin_ttm"] == 42.3
    assert parsed["revenue_growth_yoy"] == 12.1
    assert parsed["analyst_buy_count"] == 10.0
    assert parsed["analyst_hold_count"] == 4.0
    assert parsed["analyst_sell_count"] == 1.0


def test_parse_fundamentals_finnhub_missing_fields_omitted():
    parsed = parse_fundamentals_finnhub({}, [])
    assert parsed == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk proxy venv/bin/python -m pytest tests/test_fundamental_gate.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_fundamentals_finnhub'`

- [ ] **Step 3: Write minimal implementation**

Add to `trader/overlay/fundamental_gate.py` (near `fetch_fundamentals_finnhub`):

```python
def parse_fundamentals_finnhub(metrics: dict, recs: list[dict]) -> dict[str, float]:
    """Extract the same fields fetch_fundamentals_finnhub formats to text, as floats.
    Missing fields are simply absent — callers apply their own defaults."""
    out: dict[str, float] = {}
    pe = metrics.get("peBasicExclExtraTTM", metrics.get("peTTM"))
    if pe is not None:
        out["pe_ttm"] = float(pe)
    ev_fcf = metrics.get("currentEv/freeCashFlowTTM")
    if ev_fcf is not None:
        out["ev_fcf_ttm"] = float(ev_fcf)
    gross_margin = metrics.get("grossMarginTTM")
    if gross_margin is not None:
        out["gross_margin_ttm"] = float(gross_margin)
    rev_growth = metrics.get("revenueGrowthTTMYoy")
    if rev_growth is not None:
        out["revenue_growth_yoy"] = float(rev_growth)
    if recs:
        latest = recs[0]
        out["analyst_buy_count"] = float(latest.get("buy", 0))
        out["analyst_hold_count"] = float(latest.get("hold", 0))
        out["analyst_sell_count"] = float(latest.get("sell", 0))
    return out
```

- [ ] **Step 4: Run tests to verify everything passes**

Run: `rtk proxy venv/bin/python -m pytest tests/test_fundamental_gate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/overlay/fundamental_gate.py tests/test_fundamental_gate.py
git commit -m "feat(overlay): add parsed-float fundamentals extractor for feature builder"
```

---

### Task 4: `build_feature_vector` pure function

**Files:**
- Create: `trader/ml_overlay/__init__.py` (empty)
- Create: `trader/ml_overlay/features.py`
- Test: `tests/test_ml_overlay_features.py`

**Interfaces:**
- Consumes: `compute_bar_stats` (Task 1), `classify_news` output shape (Task 2),
  `parse_fundamentals_finnhub` output shape (Task 3), `SentimentSnapshot`
  (`trader/data/sentiment_client.py`), `classify_regime` (`trader/strategy/regime.py`),
  `repo.get_recent_outcomes(...)` (`trader/portfolio/repository.py:161-169`).
- Produces: `build_feature_vector(signal, bars, news_categories: dict[str, list[dict]],
  sentiment: "SentimentSnapshot | None", fundamentals: dict[str, float],
  recent_outcomes: list[dict], regime: str) -> dict[str, float]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ml_overlay_features.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk proxy venv/bin/python -m pytest tests/test_ml_overlay_features.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.ml_overlay'`

- [ ] **Step 3: Write minimal implementation**

```python
# trader/ml_overlay/__init__.py
```

```python
# trader/ml_overlay/features.py
"""Pure feature-vector builder for the ML-overlay research track.

Consumes objects the LLM overlay/gate already fetched this tick (bars, news,
sentiment, fundamentals, recent outcomes) — never re-fetches anything itself.
`bars` must already be asof-truncated (Strategy.generate's guarantee).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from trader.overlay.market_stats import compute_bar_stats
from trader.strategy.base import Signal

_NEWS_CATEGORY_KEYS = ("EARNINGS", "REGULATORY", "M&A", "ANALYST", "PRODUCT")
_REGIMES = ("calm", "normal", "stressed")

_FUNDAMENTAL_DEFAULTS = {
    "pe_ttm": 0.0,
    "ev_fcf_ttm": 0.0,
    "gross_margin_ttm": 0.0,
    "revenue_growth_yoy": 0.0,
    "analyst_buy_count": 0.0,
    "analyst_hold_count": 0.0,
    "analyst_sell_count": 0.0,
}


def build_feature_vector(
    signal: Signal,
    bars: pd.DataFrame,
    *,
    news_categories: dict[str, list[dict]],
    sentiment,  # SentimentSnapshot | None
    fundamentals: dict[str, float],
    recent_outcomes: list[dict],
    regime: str,
) -> dict[str, float]:
    """Build the numeric feature vector for one overlay decision.

    Deterministic given fixed inputs — no I/O, no clock reads except relative
    day-count math against `recent_outcomes`' own timestamps.
    """
    features: dict[str, float] = {"signal_strength": float(signal.strength)}

    bar_stats = compute_bar_stats(bars)
    features["last_close"] = bar_stats.get("last_close", 0.0)
    features["pct_20d"] = bar_stats.get("pct_20d", 0.0)
    features["vol_10d_annualized"] = bar_stats.get("vol_10d_annualized", 0.0)

    for cat in _NEWS_CATEGORY_KEYS:
        key = f"news_{cat.lower().replace('&', 'and')}_count"
        features[key] = float(len(news_categories.get(cat, [])))

    features["sentiment_bullish_ratio"] = float(sentiment.bullish_ratio) if sentiment else 0.0
    features["sentiment_mention_count"] = float(sentiment.mention_count) if sentiment else 0.0

    for key, default in _FUNDAMENTAL_DEFAULTS.items():
        features[f"fund_{key}"] = float(fundamentals.get(key, default))

    if recent_outcomes:
        features["last_trade_pnl_pct"] = float(recent_outcomes[0]["pnl_pct"])
        wins = sum(1 for o in recent_outcomes[:3] if o["pnl_pct"] > 0)
        features["win_rate_last_3"] = wins / min(len(recent_outcomes), 3)
        last_closed = datetime.fromisoformat(recent_outcomes[0]["closed_at"])
        if last_closed.tzinfo is None:
            last_closed = last_closed.replace(tzinfo=timezone.utc)
        days_since = (datetime.now(timezone.utc) - last_closed).total_seconds() / 86400.0
        features["days_since_last_trade"] = max(days_since, 0.0)
    else:
        features["last_trade_pnl_pct"] = 0.0
        features["win_rate_last_3"] = 0.0
        features["days_since_last_trade"] = -1.0

    for r in _REGIMES:
        features[f"regime_{r}"] = 1.0 if regime == r else 0.0

    return features
```

- [ ] **Step 4: Run tests to verify everything passes**

Run: `rtk proxy venv/bin/python -m pytest tests/test_ml_overlay_features.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/ml_overlay/ tests/test_ml_overlay_features.py
git commit -m "feat(ml-overlay): add build_feature_vector Phase 1 feature builder"
```

---

### Task 5: `decision_features` migration (Postgres)

**Files:**
- Create: `migrations/versions/008_decision_features.py`
- Test: manual — apply against a scratch/test Postgres or rely on existing
  migration test harness if one exists (check `tests/test_main_guards.py` /
  wherever `_run_migrations` is tested — this task only adds the migration file;
  it is exercised end-to-end by Task 6's repository tests via a real Postgres
  connection if `DATABASE_URL` is set in the test environment, otherwise skip).

- [ ] **Step 1: Write the migration**

```python
# migrations/versions/008_decision_features.py
"""Add decision_features table for ML-overlay Phase 1 feature-snapshot logging.

Revision ID: 008
Revises: 007
Create Date: 2026-07-13
"""
from __future__ import annotations
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Decision matrix at design time:
    #   - mode column distinguishes manual-queue proposals from auto executions
    #     (each row would otherwise look identical to a Phase 2 trainer; manual
    #     rows are failures from training's POV). Default 'auto' matches the
    #     dominant path; manual rows are populated by pipeline._log_decision_features.
    #   - llm_action/strength/rationale columns are DELIBERATELY omitted — see
    #     the plan's "Architecture" preamble above for the drop-vs-back-fill
    #     decision and the rationale.
    op.execute("""
        CREATE TABLE IF NOT EXISTS decision_features (
            id SERIAL PRIMARY KEY,
            run_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            strategy TEXT NOT NULL,
            regime TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'auto'
                CHECK (mode IN ('auto', 'manual')),
            signal_strength_pre_overlay REAL NOT NULL,
            features JSONB NOT NULL,
            order_id INTEGER,
            backfilled BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_features_symbol_ts "
        "ON decision_features(symbol, ts DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_features_order_id "
        "ON decision_features(order_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS decision_features")
```

- [ ] **Step 2: Verify migration chain is consistent**

Run: `rtk proxy venv/bin/python -c "
import importlib
m = importlib.import_module('migrations.versions.008_decision_features')
assert m.revision == '008' and m.down_revision == '007'
print('ok')
"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add migrations/versions/008_decision_features.py
git commit -m "feat(migrations): add decision_features table (008)"
```

---

### Task 6: `record_decision_features` + `link_order_to_decision_features` repository methods

**Files:**
- Modify: `trader/portfolio/repository.py` (add `DecisionFeaturesRow` dataclass +
  abstract methods)
- Modify: `trader/portfolio/postgres_repo.py`
- Modify: `trader/portfolio/sqlite_repo.py`
- Test: `tests/test_portfolio_repo.py` (extend existing — it already parametrizes
  over both backends per repo convention; check the file's existing fixture
  pattern before adding, mirror it exactly)

**Interfaces:**
- Consumes: nothing new beyond stdlib `json`.
- Produces:
  - `repo.record_decision_features(row: DecisionFeaturesRow) -> int` (returns new row id)
  - `repo.link_order_to_decision_features(run_id: int, order_id: int) -> None`
    (back-fills `order_id` on the most recent `decision_features` row for that
    `run_id` that doesn't already have one — `run_id` is unique per pipeline tick
    per symbol, so this is unambiguous within a single tick)

- [ ] **Step 1: Write the failing test**

First inspect the existing parametrization pattern:

```bash
rtk proxy grep -n "def repo\|@pytest.fixture\|parametrize" tests/test_portfolio_repo.py | head -20
```

Then add (adapting `repo` fixture usage to match what's found — this shows the
shape of the test, use the actual fixture name from the file):

```python
# add to tests/test_portfolio_repo.py
from trader.portfolio.repository import DecisionFeaturesRow


def test_record_decision_features_roundtrip(repo):
    run_id = repo.record_run(strategy="DipRecovery", mode="auto")
    row = DecisionFeaturesRow(
        run_id=run_id, symbol="AAPL", side="buy", strategy="DipRecovery",
        regime="normal", mode="auto", signal_strength_pre_overlay=0.8,
        features={"pe_ttm": 22.5, "vol_10d_annualized": 15.0},
    )
    row_id = repo.record_decision_features(row)
    assert isinstance(row_id, int)


def test_link_order_to_decision_features_backfills_order_id(repo):
    run_id = repo.record_run(strategy="DipRecovery", mode="auto")
    row = DecisionFeaturesRow(
        run_id=run_id, symbol="AAPL", side="buy", strategy="DipRecovery",
        regime="normal", mode="auto", signal_strength_pre_overlay=0.8,
        features={"pe_ttm": 22.5},
    )
    repo.record_decision_features(row)
    repo.link_order_to_decision_features(run_id=run_id, order_id=42)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk proxy venv/bin/python -m pytest tests/test_portfolio_repo.py -k decision_features -v`
Expected: FAIL with `ImportError: cannot import name 'DecisionFeaturesRow'`

- [ ] **Step 3: Add the dataclass and abstract methods**

Add to `trader/portfolio/repository.py` (near `TradeOutcomeRow`):

```python
@dataclass(frozen=True)
class DecisionFeaturesRow:
    """One overlay decision's feature snapshot, written before the LLM call.

    order_id is filled in later (link_order_to_decision_features) only if this
    decision actually produces a filled order in either the equity path or the
    CSP/Wheel path; stays None for holds, vetoes, and orders that never fill.

    `mode` distinguishes 'auto' (executed immediately) from 'manual' (queued
    for human approval). Phase 2 trainers must filter by mode because manual
    rows have order_id=NULL forever for a different reason than vetoes do —
    the human just deferred, not vetoed.

    The original sketch had `llm_action` / `llm_strength_post` / `llm_rationale`
    columns too. Those were dropped (see the plan preamble) — equivalent signal
    lives in SignalRow.reason ([overlay approved] <rationale> / [overlay veto]
    <rationale>) and Phase 2 can parse it if needed.
    """

    run_id: int
    symbol: str
    side: str
    strategy: str
    regime: str
    signal_strength_pre_overlay: float
    features: dict  # JSONB (Postgres) / TEXT-as-JSON (SQLite)
    mode: str = "auto"
    backfilled: bool = False
```

Add to the `PortfolioRepository` ABC (near `record_trade_outcome`):

```python
    @abstractmethod
    def record_decision_features(self, row: DecisionFeaturesRow) -> int: ...

    @abstractmethod
    def link_order_to_decision_features(self, run_id: int, order_id: int) -> None:
        """Back-fill order_id on the decision_features row for this run_id that
        doesn't already have one set. No-op if no matching row exists."""

    @abstractmethod
    def get_decision_features_by_order_id(self, order_id: int) -> dict | None: ...
```

- [ ] **Step 4: Implement in `postgres_repo.py`**

```python
    def record_decision_features(self, row: DecisionFeaturesRow) -> int:
        import json
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO decision_features "
                    "(run_id, ts, symbol, side, strategy, regime, mode, "
                    "signal_strength_pre_overlay, features, backfilled) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING id",
                    (row.run_id, _now(), row.symbol, row.side, row.strategy,
                     row.regime, row.mode,
                     row.signal_strength_pre_overlay,
                     json.dumps(row.features), row.backfilled),
                )
                return int(cur.fetchone()["id"])

    def link_order_to_decision_features(self, run_id: int, order_id: int) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE decision_features SET order_id = %s "
                    "WHERE run_id = %s AND order_id IS NULL",
                    (order_id, run_id),
                )

    def get_decision_features_by_order_id(self, order_id: int) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM decision_features WHERE order_id = %s", (order_id,)
                )
                row = cur.fetchone()
                return dict(row) if row else None
```

- [ ] **Step 5: Implement in `sqlite_repo.py`**

Add to `_SCHEMA` string in `trader/portfolio/sqlite_repo.py`:

```sql
CREATE TABLE IF NOT EXISTS decision_features (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                      INTEGER NOT NULL,
    ts                          TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    side                        TEXT NOT NULL,
    strategy                    TEXT NOT NULL,
    regime                      TEXT NOT NULL,
    mode                        TEXT NOT NULL DEFAULT 'auto',
    signal_strength_pre_overlay REAL NOT NULL,
    features                    TEXT NOT NULL,
    order_id                    INTEGER,
    backfilled                  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decision_features_symbol_ts ON decision_features(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_decision_features_order_id ON decision_features(order_id);
-- SQLite enforces CHECK constraints from CREATE TABLE IF NOT EXISTS onward; older
-- DBs that already have a decision_features table from a prior pre-edit run need
-- a re-key via a follow-up migration. Phase 1 ships fresh, so this is a non-issue
-- for the initial deploy — noted for completeness.
```

Add methods to `SQLiteRepository`:

```python
    def record_decision_features(self, row: DecisionFeaturesRow) -> int:
        import json
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO decision_features "
                "(run_id, ts, symbol, side, strategy, regime, mode, "
                "signal_strength_pre_overlay, features, backfilled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.run_id, _now(), row.symbol, row.side, row.strategy,
                 row.regime, row.mode,
                 row.signal_strength_pre_overlay,
                 json.dumps(row.features), int(row.backfilled)),
            )
            return int(cur.lastrowid)

    def link_order_to_decision_features(self, run_id: int, order_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE decision_features SET order_id = ? "
                "WHERE run_id = ? AND order_id IS NULL",
                (order_id, run_id),
            )

    def get_decision_features_by_order_id(self, order_id: int) -> dict | None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM decision_features WHERE order_id = ?", (order_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None
```

Add the `DecisionFeaturesRow` import at the top of `sqlite_repo.py`'s existing
`from trader.portfolio.repository import (...)` block.

- [ ] **Step 6: Finish the test using the new getter**

Update the Step 1 test's second assertion:

```python
def test_link_order_to_decision_features_backfills_order_id(repo):
    run_id = repo.record_run(strategy="DipRecovery", mode="auto")
    row = DecisionFeaturesRow(
        run_id=run_id, symbol="AAPL", side="buy", strategy="DipRecovery",
        regime="normal", signal_strength_pre_overlay=0.8,
        features={"pe_ttm": 22.5}, llm_action="approve",
        llm_strength_post=0.8, llm_rationale="ok",
    )
    repo.record_decision_features(row)
    repo.link_order_to_decision_features(run_id=run_id, order_id=42)
    linked = repo.get_decision_features_by_order_id(42)
    assert linked is not None
    assert linked["symbol"] == "AAPL"
    assert linked["mode"] == "auto"
```

- [ ] **Step 7: Run tests to verify everything passes**

Run: `rtk proxy venv/bin/python -m pytest tests/test_portfolio_repo.py -k decision_features -v`
Expected: PASS.

- [ ] **Step 8: Run full suite**

Run: `rtk proxy venv/bin/python -m pytest`
Expected: PASS (no regressions in existing repository tests).

- [ ] **Step 9: Commit**

```bash
git add trader/portfolio/repository.py trader/portfolio/postgres_repo.py trader/portfolio/sqlite_repo.py tests/test_portfolio_repo.py
git commit -m "feat(portfolio): add decision_features record/link repository methods"
```

---

### Task 7: Wire feature logging into the pipeline

**Files:**
- Modify: `trader/pipeline.py` (`_prepare_signal` ~line 588-779, `_execute_signal`
  ~line 789-930)
- Test: `tests/test_pipeline.py` (extend existing)

**Interfaces:**
- Consumes: `build_feature_vector` (Task 4), `repo.record_decision_features` /
  `repo.link_order_to_decision_features` (Task 6), `DecisionFeaturesRow` (Task 6).
- Produces: nothing new — this task only wires existing pieces together.

- [ ] **Step 1: Write the failing tests**

These two tests mirror the existing `_FixedStrategy` / `_run` / SQLite repo
fixture pattern in `tests/test_pipeline.py` (verified — see
`test_pipeline_auto_mode_executes` for the closest analog). Add to the same file:

```python
# tests/test_pipeline.py (add at end)
from trader.portfolio.repository import DecisionFeaturesRow
from trader.portfolio.sqlite_repo import SQLiteRepository


def test_prepare_signal_records_decision_feature_with_pre_overlay_strength(tmp_path):
    """A buy signal that reaches apply_overlay must produce exactly one
    decision_features row with signal_strength_pre_overlay == the strategy's
    pre-overlay strength (0.42 here, NOT modified by the (no-keyed) overlay in
    this test). The assertion pins the logging insertion point BEFORE
    apply_overlay so the feature vector captures the strategy's view, not
    the LLM's revised view.
    """
    cfg = _config(tmp_path, autonomy="auto")
    captured: list[DecisionFeaturesRow] = []

    def _capture(row: DecisionFeaturesRow) -> int:
        captured.append(row)
        return len(captured)  # synthesise an id, never used downstream here

    # _run() creates the SQLiteRepository inside; patch the method on the class
    # so every new instance picks up the stub for the duration of this run.
    original_record = SQLiteRepository.record_decision_features
    SQLiteRepository.record_decision_features = _capture
    try:
        results, _, _ = _run([_FixedStrategy(_SYMBOL, "buy", strength=0.42)], cfg)
    finally:
        SQLiteRepository.record_decision_features = original_record

    assert results[0].outcome == "executed"
    assert len(captured) == 1, f"expected exactly one feature snapshot, got {len(captured)}"
    assert captured[0].symbol == _SYMBOL
    assert captured[0].side == "buy"
    assert captured[0].mode == "auto"
    assert captured[0].signal_strength_pre_overlay == pytest.approx(0.42)


def test_execute_signal_links_order_id_to_decision_feature(tmp_path):
    """After _execute_signal runs repo.record_order, link_order_to_decision_features
    must set decision_features.order_id = orders.id so a Phase 2 trainer can
    join the feature vector back to the concrete fill that resulted from it.
    Mirrors the same end-to-end pattern but verifies the linking side-effect
    that Step 4 (and Step 4b for the CSP path) actually fire.
    """
    cfg = _config(tmp_path, autonomy="auto")
    results, repo, _ = _run([_FixedStrategy(_SYMBOL, "buy", strength=0.8)], cfg)
    assert results[0].outcome == "executed"

    orders = repo.get_orders()
    assert len(orders) == 1
    assert orders[0]["status"] == "filled"
    order_id = orders[0]["id"]

    df = repo.get_decision_features_by_order_id(order_id)
    assert df is not None, f"decision_features row for order_id={order_id} missing"
    assert df["symbol"] == _SYMBOL
    assert df["mode"] == "auto"
    assert df["signal_strength_pre_overlay"] == pytest.approx(0.8)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk proxy venv/bin/python -m pytest tests/test_pipeline.py -k decision_features -v`
Expected: FAIL — `record_decision_features` never called.

- [ ] **Step 3: Wire into `_prepare_signal` (with cache layer for bounded external call volume)**

In `trader/pipeline.py`, the overlay call site is:

```python
        if not signal.reason.startswith("stop-loss:") and not signal.reason.startswith("eod-exit:") and not _is_intraday:
            signal = apply_overlay(
                signal, bars, config,
                repo=repo, strategy_name=type(strategy).__name__, regime=classify_regime(bars),
            )
```

Replace with (the helper re-derives news/sentiment/fundamentals via a 60-second
in-process cache so the tick-overlap doubling is absorbed at the data-fetch layer
rather than at the overlay-signature layer; phase 1 keeps `apply_claude_overlay`'s
public signature stable for additive scope):

```python
        if not signal.reason.startswith("stop-loss:") and not signal.reason.startswith("eod-exit:") and not _is_intraday:
            regime = classify_regime(bars)
            _log_decision_features(
                config=config, repo=repo, run_id=run_id, signal=signal,
                bars=bars, strategy_name=type(strategy).__name__,
                regime=regime, mode=effective_autonomy(config),
            )
            signal = apply_overlay(
                signal, bars, config,
                repo=repo, strategy_name=type(strategy).__name__, regime=regime,
            )
```

Add the cache + helper above `_prepare_signal` in `trader/pipeline.py`:

```python
# 60-second in-process cache so _log_decision_features and apply_claude_overlay
# can both call into the underlying Finnhub helpers without doubling external
# API volume on the same tick. Finnhub free-tier is ~60 calls/min — without
# this, a tick with N equity strategies that pass the overlay gate would issue
# 2N news + 2N fundamentals calls (one each from the overlay + one each from
# here). The TTL is short enough that the next minute sees fresh data, but
# long enough to dedupe same-tick calls. Reset on process restart; the cache is
# best-effort, not authoritative.
import time as _time
_NEWS_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_FUNDAMENTALS_CACHE: dict[str, tuple[float, tuple[dict, list[dict]]]] = {}
_FETCH_TTL_S = 60.0
# Cheap bound so a long-running session touching hundreds of symbols doesn't
# grow these dicts without limit. When exceeded, drop the entire cache — a
# worst-case minute of stale data followed by a flood of fresh fetches, which
# is bounded by Finnhub's rate-limit anyway. Conservative floor — the cache
# only matters when it absorbs same-tick doubles, so 256 (way more than any
# realistic per-minute symbol count) is fine.
_MAX_FETCH_CACHE_ENTRIES = 256


def _news_cache_put(key: tuple[str, str], value: dict, now: float) -> None:
    if len(_NEWS_CACHE) > _MAX_FETCH_CACHE_ENTRIES:
        _NEWS_CACHE.clear()
    _NEWS_CACHE[key] = (now, value)


def _fundamentals_cache_put(symbol: str, value: tuple[dict, list[dict]], now: float) -> None:
    if len(_FUNDAMENTALS_CACHE) > _MAX_FETCH_CACHE_ENTRIES:
        _FUNDAMENTALS_CACHE.clear()
    _FUNDAMENTALS_CACHE[symbol] = (now, value)


def _log_decision_features(*, config, repo, run_id, signal, bars, strategy_name, regime, mode) -> None:
    """Best-effort feature-snapshot log. Never raises — must not affect the overlay.

    Re-derives news/sentiment/fundamentals via the same Finnhub client singletons
    the overlay uses, behind a 60-second in-process cache so the tick-overlap
    doubling is absorbed. The cache is preferred to threading pre-fetched
    objects through apply_overlay's signature because it keeps Phase 1
    additive — no overlay-signature change.
    """
    try:
        from trader.ml_overlay.features import build_feature_vector
        from trader.overlay import _get_finnhub_client, _get_sentiment_client
        from trader.overlay.news_context import _fetch_finnhub_articles_classified
        from trader.overlay.fundamental_gate import parse_fundamentals_finnhub
        from trader.portfolio.repository import DecisionFeaturesRow

        finnhub_client = _get_finnhub_client(config)
        news_categories: dict = {}
        finnhub_key = getattr(config, "finnhub_api_key", None)
        if finnhub_key:
            try:
                cache_key = (signal.symbol, finnhub_key)
                cached_news = _NEWS_CACHE.get(cache_key)
                if cached_news is not None and (_time.monotonic() - cached_news[0]) < _FETCH_TTL_S:
                    news_categories = cached_news[1]
                else:
                    news_categories = _fetch_finnhub_articles_classified(signal.symbol, finnhub_key)
                    _news_cache_put(cache_key, news_categories, _time.monotonic())
            except Exception:
                news_categories = {}

        sentiment = None
        if "/" in signal.symbol:
            sentiment_client = _get_sentiment_client(config, finnhub_client)
            if sentiment_client is not None:
                # SentimentClient.get_sentiment has its own 4-hour in-process
                # cache in trader/data/sentiment_client.py; no cache layer needed here.
                sentiment = sentiment_client.get_sentiment(signal.symbol)

        fundamentals: dict = {}
        if finnhub_client is not None and "/" not in signal.symbol:
            try:
                cached_f = _FUNDAMENTALS_CACHE.get(signal.symbol)
                if cached_f is not None and (_time.monotonic() - cached_f[0]) < _FETCH_TTL_S:
                    metrics, recs = cached_f[1]
                else:
                    metrics = finnhub_client.basic_financials(signal.symbol) or {}
                    recs = finnhub_client.recommendation_trends(signal.symbol) or []
                    _fundamentals_cache_put(signal.symbol, (metrics, recs), _time.monotonic())
                fundamentals = parse_fundamentals_finnhub(metrics, recs)
            except Exception:
                fundamentals = {}

        recent_outcomes = []
        if repo is not None:
            try:
                recent_outcomes = repo.get_recent_outcomes(symbol=signal.symbol, limit=3)
            except Exception:
                recent_outcomes = []

        features = build_feature_vector(
            signal, bars, news_categories=news_categories, sentiment=sentiment,
            fundamentals=fundamentals, recent_outcomes=recent_outcomes, regime=regime,
        )

        if repo is not None:
            repo.record_decision_features(DecisionFeaturesRow(
                run_id=run_id, symbol=signal.symbol, side=signal.side,
                strategy=strategy_name, regime=regime, mode=mode,
                signal_strength_pre_overlay=signal.strength,
                features=features,
            ))
    except Exception:
        logger.warning("decision-features logging failed for %s", signal.symbol, exc_info=True)
```

- [ ] **Step 4: Back-fill `order_id` at the order-write site**

In `trader/pipeline.py`'s `_execute_signal`, after the first `repo.record_order(...)`
call (~line 905-913), add:

```python
        broker_order_id = str(getattr(order, "id", "") or "")
        regime = classify_regime(bars)
        order_id = repo.record_order(OrderRow(
            client_order_id=client_order_id, symbol=symbol,
            side=signal.side, notional=risk_decision.approved_notional,
            status="submitted", broker_order_id=broker_order_id or None,
            strategy_name=type(strategy).__name__,
            regime=regime,
            signal_strength=signal.strength,
            entry_rationale=signal.reason if signal.side == "buy" else None,
        ))
        try:
            repo.link_order_to_decision_features(run_id=run_id, order_id=order_id)
        except Exception:
            logger.warning("decision-features order-link failed for %s", symbol, exc_info=True)
```

(`repo.record_order` already returns `int` in both backends per Task 6's
constraints section — this only captures the return value that was previously
discarded.)

- [ ] **Step 4b: Back-fill `order_id` for the CSP/Wheel path**

`_execute_csp_entry` ALSO calls `repo.record_order(...)` for the CSP order and
discards the return value. Without an additional `link_order_to_decision_features`
call here, every Wheel/CSP decision_features row stays `order_id = NULL`
forever — a third, distinct orphan category beyond holds/vetoes that the spec
doc's "NULL for holds/vetoes — expected, not a bug" framing does NOT cover.

In `trader/pipeline.py`'s `_execute_csp_entry`, change the existing
`repo.record_order(OrderRow(...))` call (~line 1095) so its return value is
captured, and add the link call:

```python
    order_id = repo.record_order(OrderRow(
        client_order_id=client_order_id, symbol=contract.symbol, side="sell",
        notional=collateral, status="submitted", broker_order_id=broker_order_id or None,
        strategy_name=type(strategy).__name__, regime=None,
        signal_strength=signal.strength, entry_rationale=signal.reason,
    ))
    try:
        repo.link_order_to_decision_features(run_id=run_id, order_id=order_id)
    except Exception:
        logger.warning("decision-features order-link failed for %s", contract.symbol, exc_info=True)
```

`run_id` here is the same one `_prepare_signal` minted for this strategy earlier
in the tick — so the link is unambiguous within a single tick even though CSP
orders carry `symbol=contract.symbol` (the OCC option contract symbol) rather
than `signal.symbol` (the underlying).

- [ ] **Step 4c: Test that pins Step 4b (CSP link)**

The equity-path link is covered by `test_execute_signal_links_order_id_to_decision_feature`
in Step 1 — the CSP path needs its own. Without this, an implementer could break
Step 4b without any test failing. Pin the contract: `_execute_csp_entry` must
call `repo.link_order_to_decision_features(run_id, contract_order_id)` exactly
once per run_id; without that link, the third orphan category (options orders)
silently regresses.

Add this test to `tests/test_pipeline.py` — uses the existing `_config` /
`SQLiteRepository` fixture pattern and a stand-in `AlpacaOptionsBroker` subclass
to skip the contract-selection/market-order network paths:

```python
import datetime as _dt
from dataclasses import replace as _replace
from types import SimpleNamespace
from trader.execution.options_broker import AlpacaOptionsBroker
from trader.pipeline import _execute_csp_entry
from trader.strategy.base import Signal
from trader.strategy.dip_recovery import DipRecovery


def test_csp_entry_links_decision_feature_to_options_order_id(tmp_path, monkeypatch):
    """Wheel/CSP decisions must call repo.link_order_to_decision_features so
    decision_features.order_id is set. Without this, every CSP-by-dip decision
    stays orphan-forever — a third category beyond holds/vetoes that the spec
    doc's "NULL for holds/vetoes" framing does NOT cover.

    This test pins Step 4b ONLY. The companion logging assertion
    (decision_features row inserted with mode='auto') is covered separately by
    `test_prepare_signal_records_decision_feature_with_pre_overlay_strength`
    in Step 1 — that one runs the full pipeline so `_prepare_signal`
    invokes `_log_decision_features` (which this direct call doesn't reach).
    """
    cfg = _config(tmp_path, autonomy="auto")
    cfg = _replace(cfg, risk=_replace(cfg.risk, csp_on_dip_enabled=True))

    captured_links: list[tuple[int, int]] = []

    class _FakeOptionsBroker(AlpacaOptionsBroker):
        # Bypass parent's __init__ — no API keys needed for this stub.
        def __init__(self): pass
        def select_csp_contract(self, symbol, ref_price, max_collateral):
            return SimpleNamespace(symbol=f"{symbol}260116P00100000",
                                   strike=100.0, expiry=_dt.date(2026, 1, 16))
        def check_spread(self, contract_symbol): return 0.0  # 0% spread = pass
        def sell_to_open(self, contract_symbol, client_order_id):
            return SimpleNamespace(id="broker-csp-1")

    monkeypatch.setattr(SQLiteRepository, "link_order_to_decision_features",
                        lambda self, run_id, order_id: captured_links.append((run_id, order_id)))
    monkeypatch.setattr(SQLiteRepository, "get_open_options_positions",
                        lambda self, underlying=None: [])
    monkeypatch.setattr(SQLiteRepository, "record_options_position",
                        lambda self, position: 1)

    repo = SQLiteRepository(cfg.portfolio_db_path)
    run_id = repo.record_run(strategy="DipRecovery", mode="auto")
    signal = Signal(_SYMBOL, "buy", 0.7, "dip recovery entry")

    result = _execute_csp_entry(
        signal=signal, run_id=run_id, strategy=DipRecovery(symbol=_SYMBOL),
        config=cfg, options_broker=_FakeOptionsBroker(), repo=repo,
        gate=SimpleNamespace(evaluate_options_order=lambda *a, **k:
                             SimpleNamespace(approved=True, reason="ok",
                                             approved_notional=10000.0)),
        kill_switch=SimpleNamespace(engaged=lambda: False),
        state=_healthy_state(), asof=_ASOF, ref_price=100.0,
    )

    assert result.outcome == "executed"
    assert result.is_options is True
    assert len(captured_links) == 1, "link_order_to_decision_features was not called"
    assert captured_links[0][0] == run_id
```

- [ ] **Step 5: Run tests to verify everything passes**

Run: `rtk proxy venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: PASS.

- [ ] **Step 6: Run full suite**

Run: `rtk proxy venv/bin/python -m pytest`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add trader/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): log decision_features snapshot before overlay, link order_id on fill"
```

---

### Task 8: Nightly outcome-linking job scaffold

**Files:**
- Create: `trader/learning/link_outcomes.py`
- Test: `tests/test_link_outcomes.py`

**Interfaces:**
- Consumes: `repo.get_decision_features_by_order_id` (Task 6),
  `repo.get_recent_outcomes` (existing).
- Produces: `count_linked_decision_features(repo) -> int` — counts
  `decision_features` rows with non-null `order_id` (i.e. rows that produced an
  order). Used by the Phase 1 minimum-data gate (~500 rows, ~30 losing-trade
  outcomes) before Phase 2 training starts. This task does NOT need to actually
  join `trade_outcomes` yet in a way that produces a training-ready dataset —
  that join query is genuinely Phase 2 work (it needs `orders.id` on the
  `trade_outcomes` side too, which today's `record_trade_outcome` call sites
  don't attach). Scaffolding this now would be exactly the kind of "add
  appropriate handling" placeholder the plan format forbids, so this task's
  scope is the minimum real, testable piece: a count check.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_link_outcomes.py
from trader.learning.link_outcomes import count_linked_decision_features


def test_count_linked_decision_features(repo):
    run_id = repo.record_run(strategy="DipRecovery", mode="auto")
    from trader.portfolio.repository import DecisionFeaturesRow
    repo.record_decision_features(DecisionFeaturesRow(
        run_id=run_id, symbol="AAPL", side="buy", strategy="DipRecovery",
        regime="normal", signal_strength_pre_overlay=0.8, features={},
    ))
    assert count_linked_decision_features(repo) == 0  # no order_id set yet

    repo.link_order_to_decision_features(run_id=run_id, order_id=1)
    assert count_linked_decision_features(repo) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk proxy venv/bin/python -m pytest tests/test_link_outcomes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.learning.link_outcomes'`

- [ ] **Step 3: Write minimal implementation**

Add `get_decision_features_count(self, linked_only: bool) -> int` to the
`PortfolioRepository` ABC and both backends first (same pattern as Task 6):

```python
# repository.py ABC addition
    @abstractmethod
    def get_decision_features_count(self, linked_only: bool = False) -> int:
        """Count decision_features rows. linked_only=True counts only rows
        with order_id set (i.e. decisions that produced an order)."""
```

```python
# postgres_repo.py addition
    def get_decision_features_count(self, linked_only: bool = False) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                if linked_only:
                    cur.execute("SELECT COUNT(*) AS c FROM decision_features WHERE order_id IS NOT NULL")
                else:
                    cur.execute("SELECT COUNT(*) AS c FROM decision_features")
                return int(cur.fetchone()["c"])
```

```python
# sqlite_repo.py addition
    def get_decision_features_count(self, linked_only: bool = False) -> int:
        with self._connect() as conn:
            query = "SELECT COUNT(*) AS c FROM decision_features"
            if linked_only:
                query += " WHERE order_id IS NOT NULL"
            cur = conn.execute(query)
            return int(cur.fetchone()["c"])
```

```python
# trader/learning/link_outcomes.py
"""Nightly linked-data count check for the ML-overlay research track.

Reports how many decision_features rows have produced a real order (order_id
set) — this is the Phase 1 minimum-data gate the plan requires before Phase 2
training starts (~500 linked rows, ~30 losing-trade outcomes among them). The
losing-trade count needs an orders.id <-> trade_outcomes join that doesn't
exist yet (record_trade_outcome doesn't attach an order id today) — that join
is Phase 2 scope, tracked in the plan, not silently done here.
"""
from __future__ import annotations

from trader.portfolio.repository import PortfolioRepository


def count_linked_decision_features(repo: PortfolioRepository) -> int:
    """Count decision_features rows that produced an order (order_id set)."""
    return repo.get_decision_features_count(linked_only=True)
```

- [ ] **Step 4: Run tests to verify everything passes**

Run: `rtk proxy venv/bin/python -m pytest tests/test_link_outcomes.py tests/test_portfolio_repo.py -v`
Expected: PASS.

- [ ] **Step 5: Run full suite**

Run: `rtk proxy venv/bin/python -m pytest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/learning/link_outcomes.py tests/test_link_outcomes.py trader/portfolio/repository.py trader/portfolio/postgres_repo.py trader/portfolio/sqlite_repo.py
git commit -m "feat(learning): scaffold linked decision_features count for Phase 1 data gate"
```

---

### Task 9: Apply migration 008 to prod, record Phase 1 spec doc

**Files:**
- Create: `docs/superpowers/specs/2026-07-13-ml-overlay-phase1-feature-logging.md`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Apply migration 008 to prod via Supabase MCP**

Same process as migration 004 (see commit `9b36234`'s message) — apply
`008_decision_features.py` to the live Supabase Postgres instance so
`decision_features` rows start accumulating from real paper-trading ticks.

- [ ] **Step 2: Write the spec doc**

```markdown
# ML-Overlay Phase 1: Feature-Snapshot Logging — Design

**Date:** 2026-07-13
**Status:** Shipped — logging live, dataset accumulating

## Problem

Phase 2 training needs a labeled dataset. No historical feature snapshot exists
at decision time today — only regime bucket + signal.strength + a free-text
rationale are stored. Point-in-time news/sentiment/fundamentals cannot be
backfilled (free-tier APIs don't support "as of" queries), so the dataset can
only start accumulating from when this logging ships.

## What ships

- `decision_features` table (migration 008): one row per overlay decision,
  full numeric feature vector in `features` (JSONB/TEXT), linked to the
  resulting order via `order_id` (NULL for holds/vetoes — expected, not a bug).
- `build_feature_vector` (`trader/ml_overlay/features.py`): quantitative
  (price/vol from `market_stats.py`, shared with the LLM prompt builder so the
  two never drift) + qualitative (news category counts, sentiment ratio,
  parsed fundamentals) + trade-memory + one-hot regime.
- Wired into `trader/pipeline.py::_prepare_signal` right before `apply_overlay`,
  and `order_id` back-filled in `_execute_signal` right after `record_order`.

## Known limitations (accepted, not solved)

- **Survivorship bias**: risk-gate rejections are never persisted
  (`trader/risk/gate.py` is a pure stateless function) — the dataset is biased
  toward already-approved trades. The LLM has the same blind spot today.
- **Manual-mode deferral bias**: `mode='manual'` decision_features rows have
  `order_id=NULL` forever for a DIFFERENT reason than vetoes do — the human
  just deferred the trade, not vetoed it. Phase 2 trainers MUST filter by
  `mode='auto'` before treating NULL-`order_id` rows as negative labels;
  otherwise the dataset confuses "human said maybe" with "model said no".
- **Options orders (CSP/Wheel)**: are linked to `decision_features.order_id`
  by `_execute_csp_entry` (Step 4b) — but the link is to the option CONTRACT
  symbol's `orders.id`, not the underlying's `trade_outcomes` row. Phase 2
  trainers that want to score CSP/Wheel performance need a strategy-specific
  join (contract_symbol → options_positions.opening_order_id → orders.id).
- **No historical backfill for qualitative features**: news/sentiment/
  fundamentals as of a past decision can't be reconstructed. Only
  quantitative price/vol features could theoretically be backfilled from
  stored OHLCV bars, and this ships did NOT do that backfill — every row is
  logged live going forward, `backfilled` column exists but is always `False`
  for now.
- **Post-overlay LLM output is NOT captured on the same row**: the original
  draft of `decision_features` had `llm_action` / `llm_strength_post` /
  `llm_rationale` columns. Those were DELIBERATELY dropped to keep Phase 1
  additive (capturing them requires either changing `apply_overlay`'s public
  signature OR re-running the LLM overlay in `_log_decision_features`, which
  doubles LLM cost). Equivalent signal lives in `SignalRow.reason` as
  `[overlay approved] <rationale>` or `[overlay veto] <rationale>` plain
  text — Phase 2 can parse it if needed. Re-add the columns in a Phase 2
  migration if the regex-parsing fallback isn't good enough.
- **External call volume is bounded, not eliminated**: `_log_decision_features`
  re-fetches news/fundamentals through the same Finnhub client singletons the
  overlay uses. A 60-second in-process cache absorbs same-tick doubles to a
  worst-case of ~1 call per (symbol, endpoint) per minute, but a tick that
  touches many fresh symbols will issue up to N external calls at most once
  per minute per symbol. Full single-fetch would thread pre-fetched objects
  through `apply_overlay`'s signature — deferred since it's not additive.
  Revisit if `(symbol, minute) → Finnhub 429s` ever appear in logs.

## Minimum-data gate before Phase 2

Per the plan: do not start Phase 2 training until ≥500 linked (order_id set)
`decision_features` rows exist, with ≥30 losing-trade outcomes among them.
`trader.learning.link_outcomes.count_linked_decision_features(repo)` reports
the first number; the loss-outcome count needs an `orders.id <-> trade_outcomes`
join that doesn't exist yet — that join, plus the full walk-forward training
loop, is Phase 2 scope.

## Duration

Expect months, not weeks, given ~5-20 trades/month/strategy (per the plan).
Check `count_linked_decision_features` periodically; do not start Phase 2 early.
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-13-ml-overlay-phase1-feature-logging.md
git commit -m "docs(ml-overlay): record Phase 1 feature-logging design + limitations"
```

---

## Verification per task

- Task 1: `pytest tests/test_market_stats.py tests/test_overlay.py`
- Task 2: `pytest tests/test_news_context.py tests/test_overlay.py`
- Task 3: `pytest tests/test_fundamental_gate.py`
- Task 4: `pytest tests/test_ml_overlay_features.py`
- Task 5: migration import sanity check (no live DB required)
- Task 6: `pytest tests/test_portfolio_repo.py`
- Task 7: `pytest tests/test_pipeline.py`
- Task 8: `pytest tests/test_link_outcomes.py`
- Task 9: manual — confirm `decision_features` rows appear in prod within a day
  of deploy (`SELECT COUNT(*) FROM decision_features`)
- Full suite after every task: `rtk proxy venv/bin/python -m pytest`

## Critical files

- `trader/pipeline.py` (`_prepare_signal` ~588-779, `_execute_signal` ~789-930)
- `trader/overlay/claude_overlay.py`, `trader/overlay/market_stats.py` (new),
  `trader/overlay/news_context.py`, `trader/overlay/fundamental_gate.py`,
  `trader/overlay/__init__.py`
- `trader/ml_overlay/features.py` (new)
- `trader/portfolio/repository.py`, `postgres_repo.py`, `sqlite_repo.py`
- `trader/learning/link_outcomes.py` (new), `update_weights.py` (pattern mirrored)
- `migrations/versions/007_options_positions_unique_key.py` (current head),
  `008_decision_features.py` (new)
- `docs/superpowers/specs/2026-07-13-llm-overlay-cost-measurement.md` (Phase 0
  result this phase follows from)
