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
    daily_loss_halt_enabled: bool = False   # opt-in: halt NEW BUYS after daily loss hits daily_loss_limit_pct (env: DAILY_LOSS_HALT_ENABLED)
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
    # also reweights buy-signal ranking priority. Live stays off until shadow logs validate it.
    # Shadow default on 2026-07-18: SuperTrend was running an 85% stop-out rate (22/26 closed
    # trades) with zero down-weighting because bandit_weights was never populated (shadow was off).
    bandit_weighting_shadow: bool = True    # env: BANDIT_WEIGHTING_SHADOW
    bandit_weighting_live: bool = False     # env: BANDIT_WEIGHTING_LIVE
    # Symbol cooldown — blocks new buys on a symbol for a window after a losing exit.
    # The gate already logs every rejection reason, so enabling on paper IS the shadow test.
    # Default on 2026-07-18: RXRX/NNBR were re-bought same-day right after a losing stop-out.
    symbol_cooldown_enabled: bool = True    # env: SYMBOL_COOLDOWN_ENABLED
    symbol_cooldown_seconds: int = 3600     # env: SYMBOL_COOLDOWN_SECONDS
    # Trade-memory overlay context — shadow logs what would be injected; live actually
    # appends it to the LLM prompt. Same two-stage rollout shape as bandit weighting.
    trade_memory_shadow: bool = False       # env: TRADE_MEMORY_SHADOW
    trade_memory_live: bool = False         # env: TRADE_MEMORY_LIVE
    # Options trading — both strategies opt-in and default off (env: OPTIONS_TRADING_ENABLED
    # is the master switch; the two sub-strategies below still need their own flag).
    options_trading_enabled: bool = False   # env: OPTIONS_TRADING_ENABLED
    csp_on_dip_enabled: bool = False        # env: CSP_ON_DIP_ENABLED
    wheel_strategy_enabled: bool = False    # env: WHEEL_STRATEGY_ENABLED
    options_min_open_interest: int = 100    # min OI on the near-dated chain to consider a symbol (env: OPTIONS_MIN_OPEN_INTEREST)
    options_max_spread_pct: float = 0.10    # reject option legs with wider bid-ask than this (env: OPTIONS_MAX_SPREAD_PCT)
    # Combined cap: CSP cash-collateral + CC share-value-at-risk, capped as a fraction
    # of NAV. Checked alongside (not instead of) stock/crypto position caps in the gate.
    max_options_allocation_pct: float = 0.15  # env: MAX_OPTIONS_ALLOCATION_PCT
    # Equity buy hard gates. Default on 2026-07-18: dynamic universe swept in
    # sub-$5 leveraged/inverse ETPs (SOXS, TZA, DRIP, AMDD...) whose price gets
    # blown up 10x+ by a reverse split — the split-unadjusted entry price then
    # reads as a huge (fake) unrealized gain instead of the flat/decayed real one.
    min_equity_price: float = 5.0           # reject equity buys below this (env: MIN_EQUITY_PRICE)
    block_leveraged_etfs: bool = True       # env: BLOCK_LEVERAGED_ETFS


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
    autonomy_override_path: str = "autonomy_override.flag"  # file flag overriding AUTONOMY at runtime
    risk: RiskLimits = field(default_factory=RiskLimits)
    alpaca_options_paper: bool = True        # separate flag: options can stay paper after stock goes live
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
    supabase_jwt_secret: str | None = None      # legacy HS256 verify for Supabase Auth JWTs
    supabase_url: str | None = None             # e.g. https://<ref>.supabase.co — ES256 JWKS verify

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
    _ao = (os.getenv("AUTONOMY_OVERRIDE_PATH") or "").strip() or "autonomy_override.flag"
    return Config(
        alpaca_api_key=os.getenv("ALPACA_API_KEY"),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY"),
        alpaca_paper=_env_bool("ALPACA_PAPER", default=True),
        alpaca_options_paper=_env_bool("ALPACA_OPTIONS_PAPER", default=True),
        autonomy=os.getenv("AUTONOMY", "manual").strip().lower(),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        groq_api_key=os.getenv("GROQ_API_KEY") or None,
        portfolio_db_path=_db,
        kill_switch_path=_ks,
        autonomy_override_path=_ao,
        supabase_jwt_secret=os.getenv("SUPABASE_JWT_SECRET") or None,
        supabase_url=(os.getenv("SUPABASE_URL") or "").rstrip("/") or None,
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
            daily_loss_halt_enabled=_env_bool("DAILY_LOSS_HALT_ENABLED", default=False),
            daily_loss_limit_pct=float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.03")),
            dynamic_universe=_env_bool("DYNAMIC_UNIVERSE", False),
            universe_size=int(os.getenv("UNIVERSE_SIZE", "100")),
            dynamic_crypto_universe=_env_bool("DYNAMIC_CRYPTO_UNIVERSE", False),
            # Defaults True here to match RiskLimits — load_config's fallback used to
            # hardcode False, silently overriding the dataclass default (2026-07-18: the
            # shadow/cooldown flip never actually took effect in prod because of this).
            bandit_weighting_shadow=_env_bool("BANDIT_WEIGHTING_SHADOW", True),
            bandit_weighting_live=_env_bool("BANDIT_WEIGHTING_LIVE", False),
            symbol_cooldown_enabled=_env_bool("SYMBOL_COOLDOWN_ENABLED", True),
            symbol_cooldown_seconds=int(os.getenv("SYMBOL_COOLDOWN_SECONDS", "3600")),
            trade_memory_shadow=_env_bool("TRADE_MEMORY_SHADOW", False),
            trade_memory_live=_env_bool("TRADE_MEMORY_LIVE", False),
            options_trading_enabled=_env_bool("OPTIONS_TRADING_ENABLED", False),
            csp_on_dip_enabled=_env_bool("CSP_ON_DIP_ENABLED", False),
            wheel_strategy_enabled=_env_bool("WHEEL_STRATEGY_ENABLED", False),
            options_min_open_interest=int(os.getenv("OPTIONS_MIN_OPEN_INTEREST", "100")),
            options_max_spread_pct=float(os.getenv("OPTIONS_MAX_SPREAD_PCT", "0.10")),
            max_options_allocation_pct=float(os.getenv("MAX_OPTIONS_ALLOCATION_PCT", "0.15")),
            crypto_universe_size=int(os.getenv("CRYPTO_UNIVERSE_SIZE", "10")),
            min_cash_reserve=float(os.getenv("MIN_CASH_RESERVE", "500.0")),
            max_spread_pct=float(os.getenv("MAX_SPREAD_PCT", "0.01")),
            min_equity_price=float(os.getenv("MIN_EQUITY_PRICE", "5.0")),
            block_leveraged_etfs=_env_bool("BLOCK_LEVERAGED_ETFS", True),
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
