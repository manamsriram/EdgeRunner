# Stock-Analyzer-Bot

## Deployment

| Service    | Platform | URL / Notes                          |
|------------|----------|--------------------------------------|
| Backend    | Render   | FastAPI; auto-deploys from `main`    |
| Frontend   | Vercel   | React; auto-deploys from `main`      |
| Database   | Supabase | PostgreSQL; use pooler URI (port 6543, IPv4-compatible) for `DATABASE_URL` |

### Key env vars (Render)
- `DATABASE_URL` — Supabase pooler URI (`postgresql://postgres.[REF]:[PW]@aws-0-us-east-1.pooler.supabase.com:6543/postgres`)
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`
- `FRONTEND_ORIGIN` — Vercel deployment URL (for CORS)

### Key env vars (Vercel)
- `VITE_API_BASE_URL` — Render backend URL

## Running Locally

```bash
# Backend (from repo root)
rtk proxy venv/bin/python -m api.main

# Frontend (from frontend/)
npm run dev

# Scheduler standalone (if needed)
rtk proxy venv/bin/python -m trader.scheduler
```

## Running the Scheduler

The scheduler runs **automatically** inside the FastAPI process via the `lifespan` handler (`api/main.py:63`). On Render, no separate process is needed.

For **local standalone** use (e.g. testing without the web server):

```bash
rtk proxy venv/bin/python -m trader.scheduler
```

Requires `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in `.env`. Polls every 60 s; skips ticks when market is closed.

## Tests

```bash
rtk proxy venv/bin/python -m pytest
```
