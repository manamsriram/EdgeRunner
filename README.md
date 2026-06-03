# Stock Analyzer Bot → Autonomous Trading Agent 📈

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Alpaca](https://img.shields.io/badge/Alpaca-Paper%20Trading-FFD700?style=for-the-badge&logo=alpaca&logoColor=black)](https://alpaca.markets/)
[![Claude](https://img.shields.io/badge/Claude-Overlay-D97757?style=for-the-badge&logo=anthropic&logoColor=white)](https://www.anthropic.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-Dashboard-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white)](https://streamlit.io/)
[![pandas](https://img.shields.io/badge/pandas-Backtest-150458?style=for-the-badge&logo=pandas&logoColor=white)](https://pandas.pydata.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)

---

> **A read-only stock-research bot evolving into a disciplined, paper-first autonomous trading agent** — quant strategy, hard risk guardrails, a non-load-bearing Claude overlay, and human-in-the-loop approval that flips to full autonomy with a single config flag.

---

## What this is going to do

The project began as a LangChain/LLM stock-research chatbot (still runs — see [Legacy research app](#legacy-research-app)). It is being rebuilt, phase by phase, into a **personal autonomous trading agent on Alpaca** that:

- **Executes a backtested quant strategy** (swing / daily bars, multi-day holds — sidesteps the PDT rule) on a **liquid US large-cap universe**.
- **Proves edge before risking money** — an honest backtest harness (no lookahead, fees + slippage modelled) is the keystone, not an afterthought.
- **Routes every order through one fail-closed risk gate** — allowlist, position cap, daily-loss breaker, max trades/day, no shorting, file-backed kill switch.
- **Treats the broker as the source of truth** — reconciles against Alpaca every cycle; idempotent orders so a crash/retry never double-fires.
- **Keeps a human in the loop now, autonomy later** — `AUTONOMY=manual` queues proposals for dashboard approval; flipping to `auto` is the *only* code-path difference, and it still passes the same gate + kill switch.
- **Uses Claude as garnish, not the driver** — the LLM overlay can veto or nudge confidence with a written rationale, but **never originates a trade or sets size**.

**Paper-first, and honest about it:** no system reliably "makes money." This builds an agent that runs *your* strategy with hard guardrails and **measures whether it has edge on historical + paper data before any real money**. The risk lives in the backtest's honesty, not the plumbing. Live trading stays gated in config until edge is proven.

---

## Where it is now (Phases 0–3 shipped, 45 tests green)

```text
trader/
  config.py       # env + paper/live URL swap + autonomy flag + risk limits     [0]
  data/           # historical daily bars from Alpaca (the strategy data source) [1]
  strategy/       # Strategy interface + MA-crossover & momentum/RSI             [1]
  backtest/       # bar-replay harness: decide on bar t, fill on t+1 open        [2]
  risk/           # fail-closed risk gate every order passes + kill switch       [3]
  execution/      # Alpaca broker adapter: reconcile + idempotent orders         [3]
  portfolio/      # PortfolioRepository interface + SQLite audit/approval store  [3]
```

| Phase | What it earned |
|-------|----------------|
| **0 — Security & scaffolding** | Secrets moved to a gitignored `.env`; `users.db` untracked; `trader/` package + paper/live config established. See `SECURITY.md` for the leaked-key rotation note. |
| **1 — Data + strategy** | A `Strategy` contract that *enforces* no-lookahead (truncates bars to `asof` before any subclass runs), with MA-crossover and momentum/RSI strategies fed by Alpaca daily bars. |
| **2 — Backtest harness (keystone)** | Replays bars through the **same `Strategy` interface** the live loop uses, decides on bar *t* and **fills on bar *t+1* open**, and charges fees + slippage — an honest edge estimate before real money. |
| **3 — Risk gate + execution + portfolio** | The single fail-closed checkpoint every order passes; the Alpaca adapter where the **broker is the source of truth** and orders are **idempotent**; and a `PortfolioRepository` (SQLite now, Supabase/Postgres later) sharing `users.db` with no destructive migration. |

---

## Where it is going (Phases 4–7 roadmap)

| Phase | Plan |
|-------|------|
| **4 — Decision pipeline + approval + scheduler** | `pipeline.py` spine: `tick → data → strategy → overlay → risk → decision → execute → record`. The decision gate is the *only* difference between `manual` (proposals queued for dashboard approval) and `auto`. Scheduler on Alpaca market hours; Streamlit dashboard with pending approvals, P&L, equity curve, and a kill-switch button. |
| **5 — Claude overlay (non-load-bearing)** | Anthropic SDK + prompt caching (Sonnet default) adjusts confidence / vetoes with a written rationale. It never originates a trade or sets size. |
| **6 — Observability + autonomy toggle (still paper)** | Structured logging, metrics, alerts on fills/breaker trips/errors; then `AUTONOMY=auto` **on paper only**, exercising kill switch + circuit breakers in real market hours. |
| **7 — Go-live gate (checklist, not code)** | Real money only after out-of-sample backtest edge with costs, paper reproduction over a meaningful window, and PDT-rule + paper-fill-optimism validation. |
| **Storage swap** | Replace the SQLite repo with a Supabase/Postgres `PortfolioRepository` adapter (interface already in place) for multi-device dashboards. |

**Locked decisions:** Alpaca, paper first · autonomy manual now, flag-flip to auto later · quant signals + non-load-bearing Claude overlay · liquid US large-caps · swing/daily (no PDT) · Claude (Sonnet overlay, Opus deep) · Supabase/Postgres storage.

---

## Run it

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

### Configuration (`.env`)

| Variable | Purpose | Default |
|----------|---------|---------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca paper keys | — |
| `ALPACA_PAPER` | `true` = paper (live is gated until edge is proven) | `true` |
| `RISK_ALLOWLIST` | Comma-separated tradeable symbols | 8-ticker mega-cap basket |
| `PORTFOLIO_DB_PATH` | SQLite store for orders/trades/proposals | `users.db` |
| `KILL_SWITCH_PATH` | File whose presence halts trading | `kill_switch.flag` |
| `AUTONOMY` | `manual` (approval-gated) or `auto` (Phase 6) | `manual` |
| `OPENAI_API_KEY` | Legacy research chatbot only | — |

---

## Legacy research app

The original read-only research tool still runs — a Streamlit app with SQLite login and a LangChain/GPT-4 ReAct agent that pulls prices, financials, and news and returns text analysis.

```bash
streamlit run app.py
```

Register/login, then ask questions like *"What's the outlook for Apple stock?"* or *"Show me financials and latest news for Tesla."* Core logic lives in `tools/fetch_stock_info.py` (price/news/financials tools) and `app.py` (web app + auth + query history). Its news/financials tools are reused as **LLM-overlay context** in the trading agent — never as a signal source.

---

## Architecture (target)

```text
tick (Alpaca clock)
  → data        Alpaca daily bars
  → strategy    Strategy.generate(bars, asof)  → Signal
  → overlay     Claude veto / confidence + rationale (non-load-bearing)
  → risk gate   allowlist · position cap · daily-loss · max trades · no short · kill switch
  → decision    manual → proposal queue (dashboard approval) | auto → execute
  → execute     idempotent Alpaca order (broker = source of truth, reconcile first)
  → record      orders / trades / signals / runs / proposals  (PortfolioRepository)
```

The backtest harness replays history through the **same `Strategy` + risk path** as live, so sim and live cannot diverge.

---

## Author

Sri Ram Mannam  
[GitHub](https://github.com/manamsriram) · [LinkedIn](https://www.linkedin.com/in/sri-ram-mannam-8b61aa228/)

## License

MIT License. See [`LICENSE`](LICENSE).
