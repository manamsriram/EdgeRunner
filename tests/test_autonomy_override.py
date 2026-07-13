"""File-backed autonomy override actually changes trading behaviour.

Guards the bug where the dashboard toggle set a module global the trading loop
never read, so "manual" didn't stop auto execution.
"""
from __future__ import annotations

from trader.portfolio.repository import PROPOSAL_PENDING
from trader.portfolio.sqlite_repo import SQLiteRepository
from trader.risk.gate import AutonomyOverride, effective_autonomy

from tests.test_pipeline import _SYMBOL, _FixedStrategy, _config, _run


def test_override_absent_falls_back_to_config(tmp_path):
    cfg = _config(tmp_path, autonomy="auto")
    assert effective_autonomy(cfg) == "auto"


def test_override_forces_manual_over_auto_config(tmp_path):
    cfg = _config(tmp_path, autonomy="auto")
    AutonomyOverride(cfg.autonomy_override_path).set("manual")
    assert effective_autonomy(cfg) == "manual"

    # Auto config + manual override → pipeline must queue a proposal, not execute.
    results, repo, broker = _run([_FixedStrategy(_SYMBOL, "buy")], cfg)
    assert results[0].outcome == "queued"
    assert len(broker._client.submitted) == 0
    pending = repo.list_pending_proposals()
    assert len(pending) == 1
    assert pending[0]["status"] == PROPOSAL_PENDING


def test_override_roundtrip_and_clear(tmp_path):
    ov = AutonomyOverride(str(tmp_path / "auto.flag"))
    assert ov.get() is None
    ov.set("auto")
    assert ov.get() == "auto"
    ov.set("manual")
    assert ov.get() == "manual"
    ov.clear()
    assert ov.get() is None


def test_override_garbage_forces_manual(tmp_path):
    # Fail-safe: a present-but-unrecognized override file must NOT release the brake
    # to None (fail-open) — it resolves to "manual", the safe state.
    path = tmp_path / "auto.flag"
    path.write_text("bogus")
    assert AutonomyOverride(str(path)).get() == "manual"
