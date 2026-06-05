<div align="center">

# Stock Analyzer Bot → Autonomous Trading Agent

A personal autonomous trading agent on Alpaca: backtested quant strategies, hard risk guardrails, human-in-the-loop approval, and a non-load-bearing Claude overlay.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Alpaca](https://img.shields.io/badge/Alpaca-Paper%20Trading-FFD700?style=for-the-badge&logo=alpaca&logoColor=black)](https://alpaca.markets/)
[![Claude](https://img.shields.io/badge/Claude-Sonnet%20Overlay-D97757?style=for-the-badge&logo=anthropic&logoColor=white)](https://www.anthropic.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28.0-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white)](https://streamlit.io/)
[![pandas](https://img.shields.io/badge/pandas-Backtest-150458?style=for-the-badge&logo=pandas&logoColor=white)](https://pandas.pydata.org/)
[![pytest](https://img.shields.io/badge/pytest-9.0.3-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white)](https://docs.pytest.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)

[Report Bug](https://github.com/manamsriram/Stock-Analyzer-Bot/issues) · [Request Feature](https://github.com/manamsriram/Stock-Analyzer-Bot/issues)

</div>

---

## Overview

A paper-first autonomous trading agent that executes backtested quantitative strategies on liquid US large-caps via the Alpaca API. Every order passes through a single fail-closed risk gate. Human approval is required in `manual` mode; a single config flag switches to `auto`. The Claude Sonnet overlay can veto or adjust signal confidence but never originates a trade. Edge is proven on historical + paper data before any real money is risked.

The repo also includes the original read-only research tool: a Streamlit app with a LangChain/GPT-4 ReAct agent that pulls prices, financials, and news.

---

## Features

- **Honest backtest harness** — bar-replay engine decides on bar *t* and fills on bar *t+1* open, charging fees + slippage, through the same `Strategy` interface the live loop uses. No lookahead enforced by contract.
- **Fail-closed risk gate** — every order checks allowlist, position cap, daily-loss breaker, max-trades-per-day, no-short rule, PDT guard (blocks buys at < $25k with ≥ 3 day-trades), and a file-backed kill switch.
- **Idempotent Alpaca execution** — reconciles broker state before each order; crash-safe and retry-safe with the broker as the single source of truth.
- **Human-in-the-loop / autonomy toggle** — `AUTONOMY=manual` queues proposals for Streamlit dashboard approval; `auto` skips the queue but still passes the full risk gate.
- **Claude overlay (non-load-bearing)** — Sonnet with prompt caching can veto or nudge confidence with a written rationale. Breaking-news context fetched per tick. Disabled if `ANTHROPIC_API_KEY` is unset.
- **Observability** — structured logging at configurable level; Slack webhook alerts on fills, kill-switch trips, and daily-loss breaker events (deduped per session); equity curve rendered in the dashboard from Alpaca portfolio history.
- **Go-live gate** — `scripts/go_live_gate.py` runs an out-of-sample backtest for every (symbol, strategy) combo and checks Sharpe, max drawdown, trade count, and vs-buy-and-hold thresholds; exits 0 (PASS) / 1 (FAIL). `scripts/paper_trading_report.py` summarises paper auto-mode history and produces a verdict.
- **93 tests, all offline** — risk gate, execution, portfolio, strategy, no-lookahead contract, backtest costs, overlay, scheduler, and go-live-gate logic run without Alpaca keys or network.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Broker API | alpaca-py 0.43.4 |
| Strategy signals | pandas, numpy (native indicators — no pandas-ta) |
| LLM overlay | anthropic ≥ 0.40.0 (Claude Sonnet, prompt caching) |
| Legacy research agent | LangChain, OpenAI GPT-4, yfinance 0.2.31 |
| Dashboard | Streamlit 1.28.0 |
| Storage | SQLite (`users.db`) via `PortfolioRepository` interface |
| Alerts | Slack incoming webhook |
| Testing | pytest 9.0.3 |
| Config | python-dotenv 1.0.0 |

---

## Project Layout

```text
trader/
  config.py       # env + paper/live URL swap + autonomy flag + risk limits
  alerts.py       # fire-and-forget Slack webhook alerts
  pipeline.py     # tick → data → strategy → overlay → risk → execute
  scheduler.py    # Alpaca-clock-aware market-hours loop
  data/           # Alpaca daily bars
  strategy/       # Strategy interface (no-lookahead contract) + MA-crossover & momentum/RSI
  backtest/       # bar-replay harness: decide on bar t, fill on t+1 open
  risk/           # fail-closed risk gate + kill switch
  execution/      # Alpaca broker adapter: reconcile + idempotent orders
  portfolio/      # PortfolioRepository interface + SQLite implementation
  overlay/        # Claude LLM overlay: veto / confidence adjust (non-load-bearing)
scripts/
  go_live_gate.py         # OOS backtest + threshold checks; exits 0/1/2
  paper_trading_report.py # summarises paper auto-mode SQLite history
  smoke_alpaca.py         # verify paper connectivity
  smoke_order.py          # drive one real paper order end-to-end
app.py            # legacy Streamlit research tool (LangChain + GPT-4)
tools/
  fetch_stock_info.py     # price, news, financials tools (also used as overlay context)
```

---

## Getting Started

### Prerequisites

- Python 3.12
- Alpaca paper trading account (free at alpaca.markets)
- Anthropic API key (optional — overlay is disabled if unset)

### Installation

```bash
git clone https://github.com/manamsriram/Stock-Analyzer-Bot.git
cd Stock-Analyzer-Bot
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
```

| Variable | Purpose | Default |
|----------|---------|---------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca paper keys | — |
| `ALPACA_PAPER` | `true` = paper mode | `true` |
| `RISK_ALLOWLIST` | Comma-separated tradeable symbols | 8-ticker mega-cap basket |
| `PORTFOLIO_DB_PATH` | SQLite store path | `users.db` |
| `KILL_SWITCH_PATH` | File whose presence halts trading | `kill_switch.flag` |
| `AUTONOMY` | `manual` (approval-gated) or `auto` | `manual` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` | `INFO` |
| `SLACK_WEBHOOK_URL` | Fill / kill-switch / loss alerts | — |
| `ANTHROPIC_API_KEY` | Claude overlay (leave blank to disable) | — |
| `OPENAI_API_KEY` | Legacy research chatbot only | — |

### Running

```bash
# 1. Run all tests (no keys or network needed):
pytest

# 2. Smoke-test paper connectivity:
python scripts/smoke_alpaca.py

# 3. Drive one order through the full paper path:
python scripts/smoke_order.py AAPL 50

# 4. Start the trading scheduler:
python -m trader.scheduler

# 5. Launch the Streamlit dashboard:
streamlit run app.py
```

Kill switch:
```bash
touch kill_switch.flag   # gate rejects every order
rm kill_switch.flag      # trading resumes
```

---

## Go-Live Gate

Before setting `ALPACA_PAPER=false`, complete this checklist in order:

```bash
# 1. Out-of-sample backtest — must exit 0 (PASS)
python scripts/go_live_gate.py --in-sample-end 2023-12-31

# 2. Paper trading report — must exit 0 (≥1 auto run + ≥1 fill)
python scripts/paper_trading_report.py

# 3. Kill switch manual test
#    a. touch kill_switch.flag
#    b. start scheduler — confirm it halts and logs the alert
#    c. rm kill_switch.flag — confirm trading resumes
```

Then confirm:
- Account equity ≥ $25k **or** strategy never exceeds 3 round-trips/day
- Paper fills reproduced backtest edge over ≥ 30 trading days

---

## Architecture

```text
tick (Alpaca clock)
  → data        Alpaca daily bars
  → strategy    Strategy.generate(bars, asof)  → Signal   [no-lookahead enforced]
  → overlay     Claude veto / confidence + rationale       [non-load-bearing]
  → risk gate   allowlist · position cap · daily-loss · max trades · PDT · kill switch
  → decision    manual → proposal queue (dashboard) | auto → execute
  → execute     idempotent Alpaca order (broker = source of truth)
  → record      orders / trades / signals / runs / proposals (SQLite PortfolioRepository)
```

The backtest harness replays history through the same `Strategy` + risk path as live, so simulation and live cannot diverge.

---

## Legacy Research App

The original read-only research tool: a Streamlit app with SQLite login and a LangChain/GPT-4 ReAct agent that pulls prices, financials, and news.

```bash
streamlit run app.py
```

Register/login, then ask questions like *"What's the outlook for Apple?"* or *"Show me financials and latest news for Tesla."* Its news/financials tools are reused as Claude overlay context in the trading agent — never as a signal source.

---

## Contributing

```bash
git checkout -b feature/your-feature
git commit -m "feat: describe your change"
git push origin feature/your-feature
```

Open a pull request. Run `pytest` before pushing.

---

## Author

Sri Ram Mannam  
[GitHub](https://github.com/manamsriram) · [LinkedIn](https://www.linkedin.com/in/sri-ram-mannam-8b61aa228/)

## License

[MIT](LICENSE)
