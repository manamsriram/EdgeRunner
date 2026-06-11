<div align="center">

# EdgeRunner

A paper-first autonomous trading agent on Alpaca: backtested quant strategies, hard risk guardrails, human-in-the-loop approval, and a non-load-bearing Claude overlay.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?style=for-the-badge&logo=react&logoColor=black)
![TypeScript](https://img.shields.io/badge/TypeScript-6.0-3178C6?style=for-the-badge&logo=typescript&logoColor=white)
![Alpaca](https://img.shields.io/badge/Alpaca-Paper%20Trading-FFD700?style=for-the-badge&logo=alpaca&logoColor=black)
![Claude](https://img.shields.io/badge/Claude-Sonnet%20Overlay-D97757?style=for-the-badge&logo=anthropic&logoColor=white)
![pandas](https://img.shields.io/badge/pandas-Indicators-150458?style=for-the-badge&logo=pandas&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Supabase-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)
![Render](https://img.shields.io/badge/Render-Backend-46E3B7?style=for-the-badge&logo=render&logoColor=white)
![Vercel](https://img.shields.io/badge/Vercel-Frontend-000000?style=for-the-badge&logo=vercel&logoColor=white)
![pytest](https://img.shields.io/badge/pytest-241%20tests-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)

[Live Demo](https://edgerunner-x53h.onrender.com) · [Report Bug](https://github.com/manamsriram/Stock-Analyzer-Bot/issues) · [Request Feature](https://github.com/manamsriram/Stock-Analyzer-Bot/issues)

</div>

---

## Overview

A paper-first autonomous trading agent that executes backtested quantitative strategies on US large-caps and crypto via the Alpaca API. Every order passes through a single fail-closed risk gate with eight checks. Human approval is required in `manual` mode; a config flag switches to `auto`. The Claude Sonnet overlay can veto or adjust signal confidence but never originates a trade. A live performance dashboard tracks Sharpe ratio, drawdown, and win-rate against go-live thresholds — real money is not risked until the paper account proves the edge.

---

## Features

- **Honest backtest harness** — bar-replay engine decides on bar *t* and fills on bar *t+1* open, charging fees + slippage, through the same `Strategy` interface the live loop uses. Stop-loss modeled identically to the live pipeline; no-lookahead enforced by contract.
- **Complementary strategy pair (equity)** — SuperTrend (ATR-adaptive trend-following with ADX regime filter) and DipRecovery (deep-drawdown entry / ATH-expansion exit) run in parallel per equity symbol. DipRecovery is exempt from stop-loss; its exit logic is the expansion trigger.
- **Pure Donchian crypto** — DonchianBreakout (close above prior N-bar high + trend filter) beat every tested combo on crypto across 2yr and 4yr windows. Retired EMA crossover, SmashDayB, and DipRecovery stacks are commented in-place as rollback references.
- **Backtest fidelity** — bar-replay engine decides on bar *t* and fills on bar *t+1* open, charging fees + slippage, through the same `Strategy` interface the live loop uses. Stop-loss logic mirrors the live pipeline exactly; no-lookahead enforced by contract.
- **Fail-closed risk gate** — every order checks allowlist, position cap, daily-loss breaker, max-trades-per-day, no-short rule, PDT guard (< $25k with ≥ 3 day-trades), fundamental gate, and a file-backed kill switch.
- **Idempotent Alpaca execution** — reconciles broker state before each order; crash-safe and retry-safe with the broker as single source of truth.
- **Crypto trading** — 24/7 scheduler thread runs EMA crossover, Bollinger reversion, SmashDay B, and Gap Pattern A on configured crypto symbols via the Alpaca crypto API.
- **Live performance tracker** — `trader/performance/metrics.py` computes Sharpe ratio, max drawdown, win-rate, profit factor, and trade count from live paper history (trimmed to first fill — excludes pre-trading flat period). `GET /api/performance` returns a go-live verdict (PASS/FAIL) against configurable thresholds.
- **Human-in-the-loop / autonomy toggle** — `AUTONOMY=manual` queues proposals for dashboard approval; `auto` skips the queue but still passes the full risk gate.
- **Claude overlay (non-load-bearing)** — Sonnet with prompt caching can veto or nudge confidence with a written rationale. Breaking-news context fetched per tick. Disabled if `ANTHROPIC_API_KEY` is unset.
- **Observability** — structured logging at configurable level; Slack webhook alerts on fills, kill-switch trips, and daily-loss breaker events (deduped per session); equity curve and performance metrics in the React dashboard.
- **241 tests, all offline** — risk gate, execution, portfolio, strategy, no-lookahead contract, backtest costs, overlay, scheduler, performance metrics, go-live-gate, and vol-sizing logic run without Alpaca keys or network.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Backend API | FastAPI 0.111 + uvicorn |
| Auth | JWT (PyJWT) + bcrypt |
| Broker API | alpaca-py 0.43.4 |
| Crypto | ccxt ≥ 4.3.0 |
| Strategy signals | pandas, numpy (native indicators — no pandas-ta) |
| LLM overlay | anthropic ≥ 0.40.0 (Claude Sonnet, prompt caching) |
| Frontend | React 19 + TypeScript + Vite + Tailwind CSS |
| Charts | Recharts 3.8 |
| Data fetching | TanStack React Query 5 |
| Database | SQLite (local) / PostgreSQL via Supabase (production) |
| Alerts | Slack incoming webhook |
| Hosting | Render (backend) · Vercel (frontend) |
| CI | UptimeRobot (keep-alive ping every 5 mins) |
| Testing | pytest 9.0.3 |
| Config | python-dotenv |

---

## Project Layout

```text
trader/
  config.py            # env + paper/live URL swap + autonomy flag + risk limits
  alerts.py            # fire-and-forget Slack webhook alerts
  pipeline.py          # tick → data → strategy → overlay → risk → execute
  scheduler.py         # Alpaca-clock-aware equity loop + 24/7 crypto thread
  performance/
    metrics.py         # Sharpe, drawdown, win-rate, profit factor from live history
  data/                # Alpaca daily bars + crypto bars
  strategy/
    base.py               # Strategy interface (no-lookahead contract)
    indicators.py         # SMA, RSI, EMA, Bollinger, ATR, SuperTrend, rolling high/low
    supertrend.py         # SuperTrend ATR-adaptive trend + ADX filter  [equity live]
    dip_recovery.py       # DipRecovery deep-drawdown / ATH-expansion exit [equity live]
    donchian_breakout.py  # Donchian channel breakout + trend filter     [crypto live]
    ha_pullback.py        # Heikin Ashi pullback (tested; not in live stack)
    equity_reversion.py   # Bollinger mean-reversion (tested; not in live stack)
    smash_day.py          # SmashDay Type B (Oxford Rating B; retired from live)
    gap_pattern.py        # Gap Pattern Type A (Oxford Rating B; retired from live)
    ma_crossover.py       # SMA crossover baseline (retired from live)
    crypto_trend.py       # EMA crossover for crypto (retired; rollback reference)
    regime.py             # Volatility regime detector (infrastructure; off in prod)
  universe/
    screener.py           # equity dynamic universe screener
    crypto_screener.py    # crypto dynamic universe screener
  backtest/               # bar-replay harness: decide on t, fill on t+1 open
  risk/                   # fail-closed risk gate + kill switch + fundamental gate
  execution/              # Alpaca broker adapter: reconcile + idempotent orders
  portfolio/              # PortfolioRepository interface + SQLite + Postgres impls
  overlay/                # Claude LLM overlay: veto / confidence adjust
api/
  main.py              # FastAPI app + lifespan (scheduler start)
  auth.py              # JWT auth endpoints
  routes/
    portfolio.py       # positions, orders, portfolio history
    proposals.py       # approval queue CRUD
    controls.py        # kill switch, autonomy toggle, run log
    analysis.py        # Claude-powered stock analysis
    performance.py     # live metrics + go-live verdict
  ws.py                # WebSocket price feed
frontend/src/
  pages/
    Login.tsx          # JWT login
    Portfolio.tsx      # positions + equity curve
    Approvals.tsx      # manual-mode proposal queue
    Controls.tsx       # kill switch + autonomy toggle
    Analysis.tsx       # on-demand Claude analysis
    Performance.tsx    # live metrics dashboard + go-live verdict
scripts/
  go_live_gate.py              # OOS backtest + threshold checks; exits 0/1/2
  backtest_full.py             # full IS/OOS backtest across equity universe
  backtest_combos.py           # combo harness: test strategy combinations + stop-loss exemptions
  backtest_candidates.py       # equity strategy candidate screening
  backtest_crypto.py           # crypto IS/OOS backtest
  backtest_crypto_candidates.py # crypto strategy candidate screening
  backtest_adaptive_dip.py     # adaptive DipRecovery validation harness (regime params rejected OOS)
  backtest_vol_sizing.py       # vol-targeted sizing validation harness (rejected OOS)
  paper_trading_report.py      # summarises paper auto-mode SQLite history
  performance_tracker.py       # CLI: live metrics + go-live verdict
  smoke_alpaca.py              # verify paper connectivity
  smoke_order.py               # drive one real paper order end-to-end
```

---

## Getting Started

### Prerequisites

- Python 3.12
- Node.js 18+
- Alpaca paper trading account (free at alpaca.markets)
- Anthropic API key (optional — overlay disabled if unset)

### Installation

```bash
git clone https://github.com/manamsriram/Stock-Analyzer-Bot.git
cd Stock-Analyzer-Bot
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cd frontend && npm install && cd ..
```

### Configuration

```bash
cp .env.example .env
```

| Variable | Purpose | Default |
|----------|---------|---------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca paper keys | — |
| `ALPACA_PAPER` | `true` = paper mode | `true` |
| `RISK_ALLOWLIST` | Comma-separated equity symbols | 8-ticker mega-cap basket |
| `CRYPTO_ALLOWLIST` | Comma-separated crypto symbols (e.g. `BTC/USD`) | — |
| `PORTFOLIO_DB_PATH` | SQLite store path | `users.db` |
| `KILL_SWITCH_PATH` | File whose presence halts trading | `kill_switch.flag` |
| `AUTONOMY` | `manual` (approval-gated) or `auto` | `manual` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` | `INFO` |
| `SLACK_WEBHOOK_URL` | Fill / kill-switch / loss alerts | — |
| `ANTHROPIC_API_KEY` | Claude overlay (leave blank to disable) | — |

### Running Locally

```bash
# 1. Run all tests (no keys or network needed):
pytest

# 2. Smoke-test paper connectivity:
python scripts/smoke_alpaca.py

# 3. Start the trading scheduler (equity + crypto):
python -m trader.scheduler

# 4. Start the API backend (terminal 1):
uvicorn api.main:app --reload

# 5. Start the React dashboard (terminal 2):
cd frontend && npm run dev
```

Kill switch:
```bash
touch kill_switch.flag   # gate rejects every order
rm kill_switch.flag      # trading resumes
```

---

## Strategies

Each equity symbol runs two complementary strategies in parallel; the risk gate fires at most one order per symbol per tick. Crypto runs a single strategy on a 24/7 scheduler thread.

**Equity (live stack)**

| Strategy | Signal | Stop-loss |
|----------|--------|-----------|
| SuperTrend | Close above ATR-adaptive support + ADX > 20 | ATR-based trailing stop |
| DipRecovery | Drawdown ≥ dip_pct from ATH; exit when price expands above pre-dip high | **Exempt** — exit logic is the expansion trigger |

**Crypto (live stack)**

| Strategy | Signal |
|----------|--------|
| DonchianBreakout | Close breaks above prior N-bar rolling high + trend filter |

Retired strategies (commented in scheduler.py as rollback references): MACrossover, MomentumRSI, SmashDayB, GapPatternA, CryptoEMACrossover, CryptoBollingerReversion. Tested but not deployed: HAPullback, EquityBollingerReversion.

---

## Go-Live Gate

Before setting `ALPACA_PAPER=false`, complete this checklist:

```bash
# 1. Check live performance metrics (requires paper auto-mode history):
python scripts/performance_tracker.py

# 2. Out-of-sample backtest — must exit 0 (PASS):
python scripts/go_live_gate.py --in-sample-end 2023-12-31

# 3. Paper trading report — must exit 0 (≥1 auto run + ≥1 fill):
python scripts/paper_trading_report.py
```

The performance tracker checks: Sharpe ≥ 0.5, max drawdown ≤ 20%, win-rate ≥ 40%, profit factor ≥ 1.2, trade count ≥ 10, days active ≥ 30. All thresholds must pass for a PASS verdict.

Then confirm:
- Account equity ≥ $25k **or** strategy never exceeds 3 round-trips/day
- Paper fills reproduced backtest edge over ≥ 30 trading days

---

## Architecture

```text
tick (Alpaca clock)
  → data        Alpaca daily bars (equity) / crypto bars (24/7)
  → strategy    Strategy.generate(bars, asof) → Signal   [no-lookahead enforced]
                SuperTrend + DipRecovery per equity symbol
                DonchianBreakout per crypto symbol
  → stop-loss   ATR-based trailing stop per symbol (DipRecovery exempt)
  → overlay     Claude veto / confidence + rationale       [non-load-bearing]
  → risk gate   allowlist · position cap · daily-loss · max trades · PDT
                fundamental gate · kill switch
  → decision    manual → proposal queue (dashboard) | auto → execute
  → execute     idempotent Alpaca order (broker = source of truth)
  → record      orders / trades / signals / runs / proposals (SQLite / Postgres)

performance tracker (on-demand)
  → pull fills from SQLite (trimmed to first fill — excludes pre-trading period)
  → compute Sharpe · drawdown · win-rate · profit factor · trade count
  → emit PASS / FAIL verdict against go-live thresholds
```

The backtest harness replays history through the same `Strategy` + risk path as live, so simulation and live cannot diverge.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/login` | Issue JWT |
| GET | `/api/portfolio/positions` | Open positions from Alpaca |
| GET | `/api/portfolio/orders` | Recent orders |
| GET | `/api/portfolio/history` | Equity curve data |
| GET | `/api/proposals` | Pending manual-mode proposals |
| POST | `/api/proposals/{id}/approve` | Approve a queued proposal |
| POST | `/api/proposals/{id}/reject` | Reject a queued proposal |
| GET | `/api/controls/kill-switch` | Kill switch status |
| POST | `/api/controls/kill-switch/engage` | Halt all trading |
| POST | `/api/controls/kill-switch/disengage` | Resume trading |
| GET | `/api/controls/autonomy` | Current autonomy mode |
| POST | `/api/controls/autonomy` | Set `manual` or `auto` |
| GET | `/api/controls/runs` | Recent scheduler run log |
| POST | `/api/analysis` | On-demand Claude stock analysis |
| GET | `/api/performance` | Live metrics + go-live verdict (5-min cache) |

---

## Contributing

```bash
git checkout -b feature/your-feature
git commit -m "feat: describe your change"
git push origin feature/your-feature
```

Run `pytest` before pushing — all 241 tests must pass offline (no API keys needed).

---

## Author

Sri Ram Mannam  
[GitHub](https://github.com/manamsriram) · [LinkedIn](https://www.linkedin.com/in/sri-ram-mannam-8b61aa228/)

## Validation Outcomes (2026-06-10)

Both in-tree adaptive mechanisms were stress-tested and rejected out-of-sample:

| Mechanism | In-Sample | Out-of-Sample | Decision |
|-----------|-----------|---------------|----------|
| Regime-adaptive DipRecovery params | ✅ Pass | ❌ Fail (baseline better) | Code retained, disabled in prod (`REGIME_ADAPTIVE=false`) |
| Vol-targeted position sizing (entry-fraction) | ✅ Pass | ❌ Fail (full-size wins at vol peaks) | Code retained, flat sizing in prod |

Both tests used the IS/OOS validation harness with Sharpe-gated promotion (IS Sharpe ≥ 0.5 required to evaluate OOS). Full records in `scripts/backtest_adaptive_dip.py` and `scripts/backtest_vol_sizing.py`.

---

## License

[MIT](LICENSE)
