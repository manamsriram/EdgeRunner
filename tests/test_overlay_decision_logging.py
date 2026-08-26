"""Tests for overlay_decisions capture and the ML-overlay feature transform.

The capture exists because a distillation model trained on the numeric feature
vector alone plateaued at ~73% agreement with the LLM (vs a ~96% self-consistency
ceiling) — the missing signal is the text in the prompt, which these rows preserve.
"""
import json

import pandas as pd
import pytest

from trader.ml_overlay.transform import derive
from trader.portfolio.repository import OverlayDecisionRow
from trader.portfolio.sqlite_repo import SQLiteRepository
from trader.strategy.base import Signal


def _bars(n=30):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"close": [100.0 + i for i in range(n)]}, index=idx)


def _row(**kw):
    base = dict(run_id=1, symbol="AAPL", prompt_hash="h1", action="approve",
                strength_post=0.6, rationale="looks fine", provider="gemini")
    base.update(kw)
    return OverlayDecisionRow(**base)


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------

def test_record_overlay_decision_persists_prompt_and_decision(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "t.db"))
    repo.record_overlay_decision(_row(), "the prompt text")

    with repo._connect() as conn:
        prompts = conn.execute("SELECT * FROM overlay_prompts").fetchall()
        decisions = conn.execute("SELECT * FROM overlay_decisions").fetchall()

    assert len(prompts) == 1
    assert prompts[0]["prompt_text"] == "the prompt text"
    assert len(decisions) == 1
    assert decisions[0]["action"] == "approve"
    assert decisions[0]["prompt_hash"] == "h1"
    assert decisions[0]["provider"] == "gemini"


def test_repeated_prompt_stored_once_but_each_decision_kept(tmp_path):
    """The 30-minute cache and unchanged daily features make prompt reuse the norm;
    storing the text once is what keeps this table from dominating the DB."""
    repo = SQLiteRepository(str(tmp_path / "t.db"))
    repo.record_overlay_decision(_row(run_id=1), "same prompt")
    repo.record_overlay_decision(_row(run_id=2, action="veto"), "same prompt")

    with repo._connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM overlay_prompts").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM overlay_decisions").fetchone()["c"] == 2


# ---------------------------------------------------------------------------
# overlay wiring
# ---------------------------------------------------------------------------

def _stub_llm(payload):
    return lambda *a, **kw: (json.dumps(payload), None)


def test_overlay_records_decision_when_run_id_given(tmp_path, monkeypatch):
    from trader.overlay import claude_overlay
    monkeypatch.setattr(claude_overlay, "call_llm",
                        _stub_llm({"action": "veto", "strength": 0.9, "rationale": "bad news"}))
    repo = SQLiteRepository(str(tmp_path / "t.db"))

    claude_overlay.apply_claude_overlay(
        Signal("AAPL", "buy", 0.7, "momentum crossover"), _bars(),
        None, "m", "fake-key", "claude-haiku-4-5-20251001", repo=repo, run_id=42,
    )

    with repo._connect() as conn:
        rows = conn.execute("SELECT * FROM overlay_decisions").fetchall()
        prompt = conn.execute("SELECT prompt_text FROM overlay_prompts").fetchone()["prompt_text"]

    assert len(rows) == 1
    assert rows[0]["action"] == "veto"
    assert rows[0]["rationale"] == "bad news"
    assert rows[0]["run_id"] == 42
    # The prompt must carry the text the feature vector lacks — that is the entire
    # point of storing it.
    assert "momentum crossover" in prompt


def test_no_run_id_means_no_row(tmp_path, monkeypatch):
    """Callers that don't pass run_id (tests, ad-hoc use) must not write rows."""
    from trader.overlay import claude_overlay
    monkeypatch.setattr(claude_overlay, "call_llm",
                        _stub_llm({"action": "approve", "strength": 0.5, "rationale": "ok"}))
    repo = SQLiteRepository(str(tmp_path / "t.db"))

    claude_overlay.apply_claude_overlay(
        Signal("AAPL", "buy", 0.7, "r"), _bars(),
        None, "m", "fake-key", "claude-haiku-4-5-20251001", repo=repo,
    )

    with repo._connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM overlay_decisions").fetchone()["c"] == 0


def test_logging_failure_does_not_break_overlay(monkeypatch):
    """Contract: capture is best-effort and must never affect a trading decision."""
    from trader.overlay import claude_overlay
    monkeypatch.setattr(claude_overlay, "call_llm",
                        _stub_llm({"action": "approve", "strength": 0.5, "rationale": "ok"}))

    class _BrokenRepo:
        def get_overlay_cache(self, *a, **kw): return None
        def set_overlay_cache(self, *a, **kw): return None
        def record_overlay_decision(self, *a, **kw): raise RuntimeError("db down")

    result = claude_overlay.apply_claude_overlay(
        Signal("AAPL", "buy", 0.7, "r"), _bars(),
        None, "m", "fake-key", "claude-haiku-4-5-20251001", repo=_BrokenRepo(), run_id=1,
    )

    assert result.side == "buy"
    assert "overlay approved" in result.reason


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------

def _raw(**kw):
    base = {"signal_strength": 0.7, "days_since_last_trade": -1.0,
            "regime_calm": 0.0, "regime_normal": 1.0, "regime_stressed": 0.0}
    base.update(kw)
    return base


def test_derive_drops_constant_regime_columns():
    out = derive(_raw())
    assert "regime_calm" not in out
    assert "regime_normal" not in out
    assert "regime_stressed" not in out
    assert out["signal_strength"] == 0.7


def test_derive_splits_the_no_history_sentinel():
    """-1.0 is ~85% of rows; standardising it alongside real day-counts is what the
    has_trade_history flag exists to prevent."""
    absent = derive(_raw(days_since_last_trade=-1.0))
    assert absent["has_trade_history"] == 0.0
    assert absent["days_since_last_trade"] == 0.0

    present = derive(_raw(days_since_last_trade=3.5))
    assert present["has_trade_history"] == 1.0
    assert present["days_since_last_trade"] == pytest.approx(3.5)


def test_derive_distinguishes_no_history_from_zero_days():
    """A symbol traded today (0.0) must not look identical to one never traded (-1.0)."""
    never = derive(_raw(days_since_last_trade=-1.0))
    today = derive(_raw(days_since_last_trade=0.0))
    assert never["days_since_last_trade"] == today["days_since_last_trade"] == 0.0
    assert never["has_trade_history"] != today["has_trade_history"]
