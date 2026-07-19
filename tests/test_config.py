"""Unit tests for trader.config env-var wiring of the bandit-weighting feature flags."""
from __future__ import annotations

from trader.config import load_config


def test_bandit_weighting_defaults(monkeypatch):
    # Shadow defaults True as of 2026-07-18 (see trader/config.py); live stays opt-in.
    monkeypatch.delenv("BANDIT_WEIGHTING_SHADOW", raising=False)
    monkeypatch.delenv("BANDIT_WEIGHTING_LIVE", raising=False)
    config = load_config()
    assert config.risk.bandit_weighting_shadow is True
    assert config.risk.bandit_weighting_live is False


def test_bandit_weighting_shadow_disabled_via_env(monkeypatch):
    monkeypatch.setenv("BANDIT_WEIGHTING_SHADOW", "false")
    config = load_config()
    assert config.risk.bandit_weighting_shadow is False


def test_bandit_weighting_shadow_enabled_via_env(monkeypatch):
    monkeypatch.setenv("BANDIT_WEIGHTING_SHADOW", "true")
    monkeypatch.delenv("BANDIT_WEIGHTING_LIVE", raising=False)
    config = load_config()
    assert config.risk.bandit_weighting_shadow is True
    assert config.risk.bandit_weighting_live is False


def test_bandit_weighting_live_enabled_via_env(monkeypatch):
    monkeypatch.setenv("BANDIT_WEIGHTING_LIVE", "true")
    config = load_config()
    assert config.risk.bandit_weighting_live is True
