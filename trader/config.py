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


# Starter universe: liquid US large-caps (paper fills track live closely). The risk
# gate refuses any symbol outside the allowlist, so this is the default tradeable set
# when RISK_ALLOWLIST is unset. Tuned/expanded later, not hand-picked per trade.
DEFAULT_ALLOWLIST: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "JPM", "VOO", "SPY", "QQQ",
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_allowlist(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Comma-separated tickers from env, upper/stripped. Unset/blank → `default`.
    Never silently returns an empty allowlist (that would block every order)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    symbols = tuple(s.strip().upper() for s in raw.split(",") if s.strip())
    return symbols or default


@dataclass(frozen=True)
class RiskLimits:
    """Hard guardrail numbers enforced by the Phase 3 risk gate. One home for all of
    them. Defaults are conservative placeholders; real tuning is walk-forward, later."""

    max_position_pct: float = 0.10          # max fraction of equity in one symbol
    intraday_pool_pct: float = 0.40         # fraction of equity reserved for intraday (env: INTRADAY_POOL_PCT)
    daily_loss_limit_pct: float = 0.03      # halt trading after this daily drawdown
    # None = open universe (dynamic mode); tuple = hard restriction (static mode)
    allowlist: tuple[str, ...] | None = DEFAULT_ALLOWLIST
    pdt_equity_threshold: float = 25_000.0  # PDT rule applies below this equity level
    pdt_day_trade_limit: int = 3            # max round-trips per session on small accounts
    # Crypto-specific limits (routed by is_crypto_symbol in gate.py)
    crypto_allowlist: tuple[str, ...] = ()  # e.g. ("BTC/USD", "ETH/USD")
    max_crypto_position_pct: float = 0.05   # tighter cap — crypto is more volatile
    require_daily_pnl_check: bool = True    # False for CCXT (no last_equity available)
    stop_loss_pct: float = 0.08             # exit equity/ETF position if down this fraction from avg entry
    crypto_stop_loss_pct: float = 0.05      # tighter stop for crypto — more volatile
    # Dynamic universe — replaces static allowlist with Alpaca daily screener
    dynamic_universe: bool = False          # True → use screener (env: DYNAMIC_UNIVERSE)
    universe_size: int = 100               # max symbols per day (env: UNIVERSE_SIZE)
    # Dynamic crypto universe — ranks CRYPTO_CANDIDATE_UNIVERSE by 24h volume daily
    dynamic_crypto_universe: bool = False   # True → use crypto screener (env: DYNAMIC_CRYPTO_UNIVERSE)
    crypto_universe_size: int = 10          # max crypto pairs per day (env: CRYPTO_UNIVERSE_SIZE)
    min_cash_reserve: float = 500.0         # floor kept liquid — never deployed (env: MIN_CASH_RESERVE)
    max_spread_pct: float = 0.01             # reject buys if round-trip spread cost exceeds this (env: MAX_SPREAD_PCT)
    # Contextual-bandit strategy/regime weighting — shadow mode logs only; live mode
    # also reweights buy-signal ranking priority. Stays off until shadow logs validate it.
    bandit_weighting_shadow: bool = False   # env: BANDIT_WEIGHTING_SHADOW
    bandit_weighting_live: bool = False     # env: BANDIT_WEIGHTING_LIVE
    # Symbol cooldown — blocks new buys on a symbol for a window after a losing exit.
    # The gate already logs every rejection reason, so enabling on paper IS the shadow test.
    symbol_cooldown_enabled: bool = False   # env: SYMBOL_COOLDOWN_ENABLED
    symbol_cooldown_seconds: int = 3600     # env: SYMBOL_COOLDOWN_SECONDS
    # Trade-memory overlay context — shadow logs what would be injected; live actually
    # appends it to the LLM prompt. Same two-stage rollout shape as bandit weighting.
    trade_memory_shadow: bool = False       # env: TRADE_MEMORY_SHADOW
    trade_memory_live: bool = False         # env: TRADE_MEMORY_LIVE


@dataclass(frozen=True)
class Config:
    alpaca_api_key: str | None
    alpaca_secret_key: str | None
    alpaca_paper: bool
    autonomy: str                            # "manual" (default) or "auto"
    openai_api_key: str | None
    anthropic_api_key: str | None
    portfolio_db_path: str                   # SQLite store for orders/trades/proposals
    kill_switch_path: str                    # file flag that halts the order path
    risk: RiskLimits = field(default_factory=RiskLimits)
    database_url: str | None = None              # Postgres DSN; None → SQLite
    log_level: str = "INFO"                    # passed to logging.basicConfig
    slack_webhook_url: str | None = None       # Slack-compatible webhook for alerts
    alert_email: str | None = None             # destination address for trade emails
    smtp_user: str | None = None               # sending email address (e.g. Gmail)
    smtp_password: str | None = None           # app password, not account password
    # Crypto execution config
    order_type: str = "market"                 # "market" | "limit" — limit=DAY buys at mid, sells stay market
    crypto_exchange: str = "alpaca"            # "alpaca" | "binance" | "coinbase" | "kraken"
    ccxt_api_key: str | None = None
    ccxt_secret_key: str | None = None
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    finnhub_api_key: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None

    @property
    def alpaca_base_url(self) -> str:
        return PAPER_BASE_URL if self.alpaca_paper else LIVE_BASE_URL

    def require_alpaca_credentials(self) -> None:
        """Check only that Alpaca keys are present — no paper/live guard.
        Use for data clients (bars fetching) that do not submit orders."""
        if not self.alpaca_api_key or not self.alpaca_secret_key:
            raise RuntimeError(
                "Alpaca credentials missing. Set ALPACA_API_KEY and ALPACA_SECRET_KEY."
            )

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
    _db = (os.getenv("PORTFOLIO_DB_PATH") or "").strip() or "users.db"
    _ks = (os.getenv("KILL_SWITCH_PATH") or "").strip() or "kill_switch.flag"
    return Config(
        alpaca_api_key=os.getenv("ALPACA_API_KEY"),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY"),
        alpaca_paper=_env_bool("ALPACA_PAPER", default=True),
        autonomy=os.getenv("AUTONOMY", "manual").strip().lower(),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        groq_api_key=os.getenv("GROQ_API_KEY") or None,
        portfolio_db_path=_db,
        kill_switch_path=_ks,
        risk=RiskLimits(
            # Dynamic universe: allowlist=None opens the gate to any screened symbol.
            # Static mode: allowlist populated from RISK_ALLOWLIST or the DEFAULT_ALLOWLIST.
            allowlist=None if _env_bool("DYNAMIC_UNIVERSE", False)
                      else _env_allowlist("RISK_ALLOWLIST", DEFAULT_ALLOWLIST),
            intraday_pool_pct=float(os.getenv("INTRADAY_POOL_PCT", "0.40")),
            pdt_equity_threshold=float(os.getenv("PDT_EQUITY_THRESHOLD", "25000")),
            pdt_day_trade_limit=int(os.getenv("PDT_DAY_TRADE_LIMIT", "3")),
            crypto_allowlist=None if _env_bool("DYNAMIC_CRYPTO_UNIVERSE", False)
                      else _env_allowlist("CRYPTO_ALLOWLIST", ()),
            max_crypto_position_pct=float(os.getenv("MAX_CRYPTO_POSITION_PCT", "0.05")),
            require_daily_pnl_check=_env_bool("REQUIRE_DAILY_PNL_CHECK", default=True),
            dynamic_universe=_env_bool("DYNAMIC_UNIVERSE", False),
            universe_size=int(os.getenv("UNIVERSE_SIZE", "100")),
            dynamic_crypto_universe=_env_bool("DYNAMIC_CRYPTO_UNIVERSE", False),
            bandit_weighting_shadow=_env_bool("BANDIT_WEIGHTING_SHADOW", False),
            bandit_weighting_live=_env_bool("BANDIT_WEIGHTING_LIVE", False),
            symbol_cooldown_enabled=_env_bool("SYMBOL_COOLDOWN_ENABLED", False),
            symbol_cooldown_seconds=int(os.getenv("SYMBOL_COOLDOWN_SECONDS", "3600")),
            trade_memory_shadow=_env_bool("TRADE_MEMORY_SHADOW", False),
            trade_memory_live=_env_bool("TRADE_MEMORY_LIVE", False),
            crypto_universe_size=int(os.getenv("CRYPTO_UNIVERSE_SIZE", "10")),
            min_cash_reserve=float(os.getenv("MIN_CASH_RESERVE", "500.0")),
            max_spread_pct=float(os.getenv("MAX_SPREAD_PCT", "0.01")),
        ),
        database_url=os.getenv("DATABASE_URL") or None,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL") or None,
        alert_email=os.getenv("ALERT_EMAIL") or None,
        smtp_user=os.getenv("SMTP_USER") or None,
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        order_type=os.getenv("ORDER_TYPE", "market").strip().lower(),
        crypto_exchange=os.getenv("CRYPTO_EXCHANGE", "alpaca").strip().lower(),
        ccxt_api_key=os.getenv("CCXT_API_KEY") or None,
        ccxt_secret_key=os.getenv("CCXT_SECRET_KEY") or None,
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        finnhub_api_key=os.getenv("FINNHUB_API_KEY") or None,
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID") or None,
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET") or None,
    )
