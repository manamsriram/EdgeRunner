# Stock Analyzer Bot 📈

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-App-FF4B4B?logo=streamlit&logoColor=white)](https://streamlit.io/)
[![LangChain](https://img.shields.io/badge/LangChain-AI-00B86B?logo=dataiku&logoColor=white)](https://langchain.com/)
[![OpenAI](https://img.shields.io/badge/OpenAI-GPT4-10A37F?logo=openai&logoColor=white)](https://platform.openai.com/)
[![yfinance](https://img.shields.io/badge/yfinance-Finance-73A1FB?logo=yahoo&logoColor=white)](https://pypi.org/project/yfinance/)
[![DuckDuckGo](https://img.shields.io/badge/DuckDuckGo-Search-FE7A16?logo=duckduckgo&logoColor=white)](https://duckduckgo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

> **AI-powered Bot to analyze stocks using LLMs, real-time/historical price, financials, and news.**  
> Get comprehensive investment insights from any device––no finance expertise required!

---

## 🔥 Overview

Stock Analyzer Bot uses LangChain, OpenAI LLMs, and live search/news/financial APIs to:
- Fetch & analyze **real-time/historical stock prices**
- Summarize **latest company news**
- Parse **financial statements**
- Return actionable investment analysis

---

## 🎯 Motivation

Stock analysis can be overwhelming for everyday investors. This bot centralizes data, automates analysis, and translates jargon, so you can focus on making smart decisions—faster.

---

## 🚀 Features

- **Conversational Q&A:** Ask investment questions in plain English.
- **Comprehensive Pipeline:** Prices, news, financials—all in one workflow.
- **Sentiment & Risk Analysis:** Latest market sentiment mixed with hard data.
- **Integrated Dashboard:** History, authentication, easy exploration.
- **Extensible Tools:** Add custom analysis, new data sources, or assets.

---

## 🛠️ Tech Stack

| [![Python](https://img.shields.io/badge/-Python-3A75A6?logo=python)](https://www.python.org/) | [![Streamlit](https://img.shields.io/badge/-Streamlit-FF4B4B?logo=streamlit)](https://streamlit.io/) | [![LangChain](https://img.shields.io/badge/-LangChain-29B88A?logo=dataiku)](https://langchain.com/) | [![OpenAI](https://img.shields.io/badge/-OpenAI-10A37F?logo=openai)](https://openai.com/) | [![yfinance](https://img.shields.io/badge/-yfinance-73A1FB?logo=yahoo)](https://github.com/ranaroussi/yfinance) | [![DuckDuckGo](https://img.shields.io/badge/-DuckDuckGo-FE7A16?logo=duckduckgo)](https://duckduckgo.com/) | [![BeautifulSoup](https://img.shields.io/badge/-BeautifulSoup-4E8B93?logo=pypi)](https://www.crummy.com/software/BeautifulSoup/) | [![Pandas](https://img.shields.io/badge/-Pandas-150458?logo=pandas)](https://pandas.pydata.org/) |
|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|

---

## 📦 Installation

git clone https://github.com/manamsriram/Stock-Analyzer-Bot.git
cd Stock-Analyzer-Bot

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt


Add your OpenAI API key to a `.env` file:

OPENAI_API_KEY=sk-xxxxxxx


---

## 🖥️ Usage

streamlit run app.py

Open the Streamlit link in your browser, register/login, and start asking investment questions!

---

## 🗂️ Example Queries

- "Should I invest in Tesla this month?"
- "Show me financials and latest news for Yes Bank."
- "What's the outlook for Apple stock?"

---

## 🧰 Notebooks & Core Logic

- **stock_analyzer_bot.ipynb** : Tool definitions, LLM workflow examples, testing
- **app.py** : Streamlit web app, user authentication, query storage
- **tools/fetch_stock_info.py** : All major tools for price, news, financials

---

## 🏗️ Roadmap

- Add: Technical analysis, charting
- Support: More open-source LLMs (Claude, Mistral, Llama, ...), new markets
- Deploy: HuggingFace Spaces, Streamlit Cloud
- Modular: Crypto, mutual funds, asset categories
- Enhanced: Visual dashboard, search tools

---

## 🤖 Trading agent (paper-first)

The `trader/` package evolves this read-only research tool into a disciplined,
**paper-first Alpaca trading agent**. It is built in phases so guardrails exist *before*
any automation. **Phases 0–3 are implemented (40 tests green); phases 4–7 are a
documented roadmap.**

### Current state

```text
trader/
  config.py       # env + paper/live URL swap + autonomy flag + risk limits   [0]
  data/           # historical daily bars from Alpaca (the strategy data source) [1]
  strategy/       # Strategy interface + MA-crossover & momentum/RSI            [1]
  backtest/       # bar-replay harness: decide on bar t, fill on t+1 open       [2]
  risk/           # fail-closed risk gate every order passes + kill switch      [3]
  execution/      # Alpaca broker adapter: reconcile + idempotent orders        [3]
  portfolio/      # PortfolioRepository interface + SQLite audit/approval store [3]
```

What each phase earned:

- **Phase 0 — Security & scaffolding.** Secrets moved to a gitignored `.env`; `users.db`
  untracked; the `trader/` package and paper/live config established. See `SECURITY.md`
  for the leaked-key rotation note.
- **Phase 1 — Data + strategy.** A `Strategy` contract that *enforces* no-lookahead
  (truncates bars to `asof` before any subclass runs), with MA-crossover and momentum/RSI
  strategies fed by Alpaca daily bars.
- **Phase 2 — Backtest harness (keystone).** Replays bars through the **same `Strategy`
  interface** the live loop uses, decides on bar *t* and **fills on bar *t+1* open** (no
  lookahead), and charges fees + slippage — an honest edge estimate before real money.
- **Phase 3 — Risk gate + execution + portfolio (order path).**
  - **Risk gate** is the single checkpoint *every* order — manual or auto — must pass:
    allowlist, max position size (buys sized down to the cap), daily-loss circuit breaker,
    max trades/day, no shorting, and a file-backed **kill switch**. It is **fail-closed** —
    a failed reconciliation or unknown daily P&L *rejects* rather than trades blind.
  - **Execution** wraps Alpaca; the **broker is the source of truth for positions**
    (`reconcile()` on every cycle), and orders are **idempotent** via a deterministic
    `client_order_id` so a crash/retry can never double-fire.
  - **Portfolio** persists orders/trades/signals/runs/proposals behind a
    `PortfolioRepository` interface (local SQLite now, Supabase/Postgres later) — sharing
    `users.db` with no destructive migration of existing user data.

### Run it

```bash
git clone https://github.com/manamsriram/Stock-Analyzer-Bot.git
cd Stock-Analyzer-Bot
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # add Alpaca PAPER keys (ALPACA_PAPER=true)

# 1) Run the test suite (no keys/network needed — risk gate, execution, portfolio,
#    strategy, no-lookahead, and backtest costs are all offline):
pytest

# 2) Smoke-test paper connectivity (needs Alpaca paper keys):
python scripts/smoke_alpaca.py   # prints paper account + one AAPL bar

# 3) Drive ONE order through the real paper order path (places a small paper order;
#    reconcile -> risk gate -> idempotent submit -> record, then proves idempotency):
python scripts/smoke_order.py AAPL 50

# Kill switch: create the flag file to halt the order path, delete it to resume.
touch kill_switch.flag        # gate now rejects every order
rm kill_switch.flag
```

The legacy research app still runs with `streamlit run app.py`.

### Configuration (`.env`)

| Variable | Purpose | Default |
|----------|---------|---------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca paper keys | — |
| `ALPACA_PAPER` | `true` = paper (live is gated until edge is proven) | `true` |
| `RISK_ALLOWLIST` | Comma-separated tradeable symbols | 8-ticker mega-cap basket |
| `PORTFOLIO_DB_PATH` | SQLite store for orders/trades/proposals | `users.db` |
| `KILL_SWITCH_PATH` | File whose presence halts trading | `kill_switch.flag` |
| `AUTONOMY` | `manual` (approval-gated) or `auto` (Phase 6) | `manual` |

### Next steps (roadmap)

- **Phase 4 — Decision pipeline + human-in-loop approval.** `pipeline.py` spine
  (`tick → data → strategy → overlay → risk → decision → execute`); the decision gate is
  the *only* difference between `manual` (proposals queued for dashboard approval) and
  `auto`. Scheduler on Alpaca market hours; Streamlit dashboard with pending approvals,
  P&L, equity curve, and a kill-switch button.
- **Phase 5 — Claude overlay (non-load-bearing).** Anthropic SDK + prompt caching (Sonnet
  default) adjusts confidence / vetoes with a written rationale — it never originates a
  trade or sets size.
- **Phase 6 — Observability + autonomy toggle (still paper).** Structured logging, alerts,
  then `AUTONOMY=auto` on paper, exercising kill switch + circuit breakers in market hours.
- **Phase 7 — Go-live gate (checklist, not code).** Real money only after out-of-sample
  backtest edge with costs, paper reproduction, and PDT-rule validation.
- **Storage:** swap the SQLite repo for a Supabase/Postgres `PortfolioRepository` adapter
  (interface already in place) for multi-device dashboards.

> **Honest framing:** no system reliably "makes money." This builds an agent that executes
> *your* strategy with hard guardrails and **proves whether it has edge on historical +
> paper data before any real money** — the risk lives in the backtest's honesty, not the
> plumbing.

---

## 👤 Author

Sri Ram Mannam  
[GitHub](https://github.com/manamsriram) | [LinkedIn](https://www.linkedin.com/in/sri-ram-mannam-8b61aa228/)

---

## 📜 License

MIT License. See [`LICENSE`](LICENSE).

---

## 🌐 Demo

Try it live: [stock-analyzer-bot.vercel.app](https://stock-analyzer-bot.vercel.app)

---
