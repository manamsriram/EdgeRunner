"""Central configuration: secrets, paper/live wiring, autonomy flag, risk limits.

Everything that distinguishes paper from live, or manual from autonomous, funnels
through here so later phases flip behaviour with a single config change rather than
scattered edits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load .env once on import. Real values live in the gitignored .env (see .env.example).
load_dotenv()

# Alpaca REST base URLs. paper/live is a URL swap only — nothing else changes.
PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RiskLimits:
    """Placeholder guardrail numbers. Real values are pinned when Phase 3 (the risk
    gate) lands; they live here now so there is exactly one home for them."""

    max_position_pct: float = 0.10          # max fraction of equity in one symbol
    max_trades_per_day: int = 5             # circuit breaker on churn
    daily_loss_limit_pct: float = 0.03      # halt trading after this daily drawdown
    allowlist: tuple[str, ...] = ()         # empty = no symbol allowed until set


@dataclass(frozen=True)
class Config:
    alpaca_api_key: str | None
    alpaca_secret_key: str | None
    alpaca_paper: bool
    autonomy: str                            # "manual" (default) or "auto"
    openai_api_key: str | None
    anthropic_api_key: str | None
    risk: RiskLimits = field(default_factory=RiskLimits)

    @property
    def alpaca_base_url(self) -> str:
        return PAPER_BASE_URL if self.alpaca_paper else LIVE_BASE_URL

    def require_alpaca(self) -> None:
        """Fail loudly if Alpaca credentials are missing. Especially important before
        ever touching a live account."""
        if not self.alpaca_api_key or not self.alpaca_secret_key:
            mode = "paper" if self.alpaca_paper else "LIVE"
            raise RuntimeError(
                f"Alpaca {mode} credentials missing. Set ALPACA_API_KEY and "
                "ALPACA_SECRET_KEY in your .env (see .env.example)."
            )
        if not self.alpaca_paper:
            # A deliberate speed bump on the road to real money.
            raise RuntimeError(
                "ALPACA_PAPER is false (LIVE trading). Live mode is intentionally "
                "gated: prove edge in backtest + paper first, then remove this guard."
            )


def load_config() -> Config:
    return Config(
        alpaca_api_key=os.getenv("ALPACA_API_KEY"),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY"),
        alpaca_paper=_env_bool("ALPACA_PAPER", default=True),
        autonomy=os.getenv("AUTONOMY", "manual").strip().lower(),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
    )
