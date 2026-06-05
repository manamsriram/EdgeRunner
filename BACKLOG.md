# Backlog — Deferred & Out-of-Scope Items

Items identified during the Phase 8 senior code review that are real improvements but deferred to keep scope bounded. Each entry includes a fix description and rough effort.

---

## Security

### Rate Limiting on Auth Endpoints
- **Risk:** Brute-force on `POST /auth/login` and `POST /auth/register`
- **Fix:** Add `slowapi` (`pip install slowapi`); decorate login/register with `@limiter.limit("5/15minutes")`; return 429 on breach
- **Effort:** ~30 min

### WebSocket Token Expiry
- **Risk:** If the access token expires mid-session the WebSocket stays open, but the token is invalid
- **Fix:** Server periodically sends `{"event": "auth_expired"}`; client re-validates via `/auth/me` and reconnects with a fresh token
- **Effort:** ~2 hr

---

## Correctness

### Proposal Approval Race Condition
- **Risk:** Two simultaneous approve clicks could submit duplicate orders (window between `_get_pending_proposal()` and `set_proposal_status()`)
- **Fix:** Replace two-step read+update with a single atomic `UPDATE proposals SET status='approved' WHERE id=? AND status='pending'`; check `cursor.rowcount == 1` before proceeding — reject with 409 if 0
- **Effort:** ~1 hr

### Analysis Timeout
- **Risk:** If the LangChain agent hangs the SSE stream stays open indefinitely (zombie connection)
- **Fix:** Wrap `loop.run_in_executor(None, Analyze_stock, body.query)` with `asyncio.wait_for(..., timeout=120)`; yield an error SSE event on `asyncio.TimeoutError`
- **Effort:** ~20 min

---

## Developer Experience

### Query History Pagination
- **Risk:** Users with large history get slow responses and unbounded memory in one response
- **Fix:** Add `?limit=50&offset=0` query params to `GET /auth/history`; update `get_user_history()` in `deps.py` to pass them through
- **Effort:** ~30 min

### Config-Driven WebSocket Poll Interval
- **Risk:** Changing the 3-second poll interval requires a code edit
- **Fix:** Add `WS_POLL_INTERVAL_SECONDS=3` to `.env.example` and read it in `ws.py` via `int(os.getenv("WS_POLL_INTERVAL_SECONDS", "3"))`
- **Effort:** ~15 min

---

## Infrastructure

### Database Migrations (Alembic)
- **Risk:** New deployments fail silently if `users.db` schema hasn't been pre-created; schema changes require manual SQL
- **Fix:** Add `alembic` for migration management; convert the inline `CREATE TABLE` statements in `SQLiteRepository` to migration scripts; add a `alembic upgrade head` step to the startup sequence
- **Effort:** ~4 hr
