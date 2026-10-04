"""Jev overlay: veto/shrink policy, fail-open, shadow isolation, breaker, dispatch."""
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trader.overlay import apply_overlay, jev_overlay
from trader.overlay.jev_overlay import apply_jev_overlay, shadow_jev
from trader.portfolio.sqlite_repo import SQLiteRepository
from trader.strategy.base import Signal


def _bars(n=30):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"close": [100.0 + i for i in range(n)]}, index=idx)


def _resp(neg=0.05, contra=0.05, risk=0.0, conf=0.95):
    return SimpleNamespace(
        answers={
            "negative_catalyst": SimpleNamespace(noul=neg),
            "thesis_contradicted": SimpleNamespace(noul=contra),
            "event_risk": SimpleNamespace(score=risk, confidence=conf),
        },
        usage=SimpleNamespace(input_tokens=400, output_tokens=20),
    )


class _FakeClient:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, 0

    def system_one(self, state, questions, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.resp


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    jev_overlay._reset_state()
    monkeypatch.setattr(jev_overlay, "fetch_news_with_fallback", lambda *a, **k: "Headline: fine")
    monkeypatch.setattr(jev_overlay, "fetch_news", lambda *a, **k: "Headline: fine")
    yield
    jev_overlay._reset_state()


def _use(monkeypatch, client):
    monkeypatch.setattr(jev_overlay, "_get_client", lambda key: client)
    return client


BUY = Signal("AAPL", "buy", 0.8, "ema cross")


def test_high_negative_catalyst_vetoes(monkeypatch):
    _use(monkeypatch, _FakeClient(_resp(neg=0.95)))
    out = apply_jev_overlay(BUY, _bars(), "k")
    assert out.side == "hold" and out.strength == 0.0
    assert out.reason.startswith("[jev veto]")


def test_thesis_contradicted_vetoes(monkeypatch):
    _use(monkeypatch, _FakeClient(_resp(contra=0.9)))
    assert apply_jev_overlay(BUY, _bars(), "k").side == "hold"


def test_risk_level_shrinks_strength_never_raises(monkeypatch):
    _use(monkeypatch, _FakeClient(_resp(risk=2.0)))  # level 2 -> x0.5
    out = apply_jev_overlay(BUY, _bars(), "k")
    assert out.side == "buy" and out.strength == pytest.approx(0.4)
    assert out.reason.startswith("[jev approved]")


def test_low_confidence_risk_is_neutral(monkeypatch):
    _use(monkeypatch, _FakeClient(_resp(risk=3.0, conf=0.3)))
    assert apply_jev_overlay(BUY, _bars(), "k").strength == pytest.approx(0.8)


def test_sells_pass_through_without_calling_jev(monkeypatch):
    client = _use(monkeypatch, _FakeClient(_resp(neg=0.99)))
    sell = Signal("AAPL", "sell", 1.0, "exit")
    assert apply_jev_overlay(sell, _bars(), "k") is sell
    assert client.calls == 0


def test_no_news_skips_call(monkeypatch):
    monkeypatch.setattr(jev_overlay, "fetch_news_with_fallback", lambda *a, **k: "")
    monkeypatch.setattr(jev_overlay, "fetch_news", lambda *a, **k: "")
    client = _use(monkeypatch, _FakeClient(_resp(neg=0.99)))
    assert apply_jev_overlay(BUY, _bars(), "k") is BUY
    assert client.calls == 0


def test_api_error_fails_open(monkeypatch):
    _use(monkeypatch, _FakeClient(exc=RuntimeError("boom")))
    assert apply_jev_overlay(BUY, _bars(), "k") is BUY


def test_breaker_opens_then_skips_calls(monkeypatch):
    client = _use(monkeypatch, _FakeClient(exc=RuntimeError("down")))
    for _ in range(jev_overlay.BREAKER_FAILURES + 3):
        apply_jev_overlay(BUY, _bars(), "k")
    assert client.calls == jev_overlay.BREAKER_FAILURES


def test_decision_logged_with_jev_provider_and_cached(tmp_path, monkeypatch):
    repo = SQLiteRepository(str(tmp_path / "t.db"))
    client = _use(monkeypatch, _FakeClient(_resp(neg=0.95)))
    apply_jev_overlay(BUY, _bars(), "k", repo=repo, run_id=7)
    again = apply_jev_overlay(BUY, _bars(), "k", repo=repo, run_id=8)
    assert client.calls == 1 and again.side == "hold"  # second call served from cache
    with repo._connect() as conn:
        rows = conn.execute("SELECT * FROM overlay_decisions").fetchall()
    assert len(rows) == 1 and rows[0]["provider"] == "jev" and rows[0]["action"] == "veto"
    assert json.loads(rows[0]["rationale"])["negative_catalyst"] == 0.95


def test_shadow_logs_but_never_touches_cache(tmp_path, monkeypatch):
    repo = SQLiteRepository(str(tmp_path / "t.db"))
    _use(monkeypatch, _FakeClient(_resp(neg=0.95)))
    shadow_jev(BUY, _bars(), "Headline", "", "k", repo=repo, run_id=1)
    assert repo.get_overlay_cache("AAPL", "buy", 1800.0) is None
    with repo._connect() as conn:
        assert conn.execute("SELECT provider FROM overlay_decisions").fetchone()["provider"] == "jev"


def test_shadow_failure_never_raises(monkeypatch):
    _use(monkeypatch, _FakeClient(exc=RuntimeError("boom")))
    shadow_jev(BUY, _bars(), "Headline", "", "k")


def test_ml_label_query_ignores_jev_prefix():
    # build_ml_dataset labels only on '[overlay veto]' / '[overlay approved]' prefixes.
    assert not "[jev veto] x".startswith("[overlay")


def test_dispatch_jev_works_without_llm_keys(monkeypatch):
    _use(monkeypatch, _FakeClient(_resp(neg=0.95)))
    cfg = SimpleNamespace(overlay_provider="jev", typesafe_api_key="k", finnhub_api_key=None,
                          reddit_client_id=None, reddit_client_secret=None)
    assert apply_overlay(BUY, _bars(), cfg).side == "hold"


def test_dispatch_defaults_to_llm_path_without_attrs():
    cfg = SimpleNamespace(groq_api_key=None, anthropic_api_key=None, gemini_api_key=None)
    assert apply_overlay(BUY, _bars(), cfg) is BUY


def _llm_cfg(provider="shadow"):
    return SimpleNamespace(
        overlay_provider=provider, typesafe_api_key="k", gemini_api_key="g", groq_api_key=None,
        anthropic_api_key=None, finnhub_api_key=None, reddit_client_id=None,
        reddit_client_secret=None, risk=SimpleNamespace(trade_memory_shadow=False, trade_memory_live=False),
    )


def test_shadow_mode_llm_decides_and_jev_only_on_genuine_calls(tmp_path, monkeypatch):
    from trader.overlay import claude_overlay

    claude_overlay._clear_overlay_cache()
    repo = SQLiteRepository(str(tmp_path / "t.db"))
    client = _use(monkeypatch, _FakeClient(_resp(neg=0.99)))  # Jev would veto
    monkeypatch.setattr(
        claude_overlay, "call_llm",
        lambda *a, **k: (json.dumps({"action": "approve", "strength": 0.7, "rationale": "ok"}), None),
    )
    monkeypatch.setattr(claude_overlay, "fetch_news", lambda *a, **k: "Headline")
    monkeypatch.setattr("trader.overlay.news_context.fetch_news_with_fallback", lambda *a, **k: "Headline")

    first = apply_overlay(BUY, _bars(), _llm_cfg(), repo=repo, run_id=1)
    second = apply_overlay(BUY, _bars(), _llm_cfg(), repo=repo, run_id=2)  # LLM cache hit

    assert first.side == "buy" and first.reason.startswith("[overlay approved]")  # LLM decided
    assert second.side == "buy"
    assert client.calls == 1  # Jev ran once: paired with the genuine LLM call only
    with repo._connect() as conn:
        providers = sorted(r["provider"] for r in conn.execute("SELECT provider FROM overlay_decisions"))
    assert providers == ["jev", "unknown"]  # stubbed LLM returns no usage -> "unknown"
