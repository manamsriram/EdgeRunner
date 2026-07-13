# EdgeRunner Roadmap — Remaining Work

Follow-up to the 2026-07-11 audit (plan: `check-the-current-trading-linear-minsky.md`).
P0 safety + P1 validation + P3.1 options-on-paper shipped in PR #12 / commit `d6b8cb1`
(see `BACKLOG.md` → "Audit Remediation"). This file tracks everything deferred.

Ordering is roughly by risk-reduction-per-effort. Each item lists the files to touch,
the approach, and the test that proves it works.

---

## P1.4 (carryover) — Crypto cost rerun ✅ DONE

**Status:** rerun 2026-07-12 with live Alpaca keys. Output table committed at
`docs/crypto_backtest_25bps.md`; `scheduler.py` `_build_crypto_strategies_for` docstring
numbers match it (2yr Donchian 45.6/0.39/-36.3, 4yr 170.5/0.40/-43.3). Pure Donchian
dominates on both windows at 25bps — production choice confirmed under realistic cost.

<details><summary>original P1.4 spec</summary>

**Why:** Strategy-selection conclusions in `scheduler.py` docstrings and crypto notes were
drawn at 5–10 bps. Alpaca live crypto taker is 15–25 bps. The comparison must survive
realistic cost or the chosen strategy is an artifact.

**Plan:**
- Run `scripts/backtest_crypto_candidates.py` (already `taker_fee_bps=25.0`) and
  `scripts/backtest_crypto.py` on the current candidate set.
- Rerun the Donchian-vs-combo comparison; capture Sharpe / max-DD / trade count at 25 bps.
- Update the combo numbers in `trader/scheduler.py` docstrings and any crypto strategy notes
  in the same commit so code comments match the evidence.

**Verify:** committed backtest output table at 25 bps; docstring numbers match it.
**Skip for now:** live sizing haircut — paper Alpaca charges no fee; revisit at real-money go-live (P4).

</details>

---

## P2 — Hygiene & Hardening

### P2.1 Concurrency & lifecycle guards ✅ DONE

Shipped: multi-worker guard (`api/main._multi_worker()` — schedulers skip + CRITICAL log
under `WEB_CONCURRENCY>1`), bars-cache `threading.Lock` (`alpaca_bars.py`), WS token-expiry
close at `exp` with code 1008 (`api/ws.py`), and a once/day quote-failure alert
(`pipeline.py` — `get_live_prices_batch` now propagates instead of swallowing, so the stale
fallback is no longer silent). Tests: `test_main_guards.py`, `test_alpaca_bars.py`,
`test_ws.py`, `test_pipeline.py::test_quote_failure_alerts_once_per_day`.

<details><summary>original P2.1 spec</summary>

**Multi-worker guard.** Scheduler runs in-process via FastAPI lifespan and assumes exactly
one process. `--workers 2` (or `WEB_CONCURRENCY>1`) would double-submit every order.
- Refuse scheduler start when `os.getenv("WEB_CONCURRENCY", "1") != "1"` → log CRITICAL + skip
  scheduler startup (web still serves). `api/main.py` lifespan, near scheduler launch.
- Test: set `WEB_CONCURRENCY=2`, assert scheduler thread not started + one CRITICAL log line.

**Bars-cache thread safety.** Module-global bar caches (`trader/data/alpaca_bars.py` and
siblings) are read/written from equity, crypto, and trade-stream threads with no lock.
- Wrap cache get/set in a module-level `threading.Lock` (double-checked, matches the existing
  lazy-init pattern in the codebase). `# ponytail: one global lock per cache module; shard by
  symbol only if lock contention shows up in profiling.`
- Test: two threads hammering the same symbol → no `KeyError`/torn read; cache populated once.

**WS token expiry.** WebSocket connection outlives the JWT that authorized it (`api/ws.py:72-81`).
- On accept, decode `exp`; schedule close (or reject next frame) at expiry with code 1008.
- Test: token with `exp` 1s out → connection closed shortly after; fresh token stays open.

**Quote-failure alert (measure first).** Live-quote failure currently falls back to yesterday's
close for stop evaluation (`pipeline.py:184-186`) — silent staleness.
- Add a once-per-day `send_alert` when the live quote fetch fails and eval uses the stale close.
  Do NOT add skip/halt logic yet — measure frequency first, then decide.
- Test: force quote fetch to raise → exactly one alert/day, eval still proceeds on stale close.

</details>

### P2.2 Dead-code sweep (approved in audit) ✅ DONE

Deleted: pairs pipeline (`run_pair_pipeline`/`_run_pair`/`_notional_for_side`, ~205 lines;
kept `_fetch_bars`, still used by the live single-signal path), `record_trade`/`TradeRow`
write path (DB `trades` table kept — no destructive migration), `InsufficientQtyError`,
unused imports across touched files. Commented rollback stacks in `scheduler.py` replaced
with `git show <hash>^` pointer comments. `_rank_key` now precomputes each regime once
before the sort instead of per comparison. BACKLOG.md auth entry corrected (JWT verify is
live via JWKS ES256). `buy_to_close`/`exercise_options_position` kept for P3.2.

<details><summary>original P2.2 spec</summary>

Run full `pytest` after **each** deletion group; commit per group.
- **Pairs pipeline** — ~200 lines never called (`pipeline.py:966-1159`). Delete.
- **`record_trade` / `TradeRow` / trades table writes** — never written. Delete the code path.
  **Keep the DB table** (no destructive migration; drop deferred to P4).
- **`InsufficientQtyError`** — unused. Delete.
- **Commented rollback stacks** in `scheduler.py` — replace with a commit-hash pointer comment.
- **Unused imports** across touched files.
- **`_rank_key`** recomputes `classify_regime` per sort comparison (`pipeline.py:277`) —
  precompute regime once before the sort (perf, not deletion).
- **Docs:** fix the BACKLOG.md ↔ auth-code contradiction (backlog said auth removed; JWT verify
  is live via JWKS ES256 — see commit `e85326f`).
- **KEEP:** `buy_to_close` / `exercise_options_position` — needed for P3.2 roll logic.

**Verify:** suite green after each group; `git grep` confirms deleted symbols have no references.

</details>

### P2.3 IC observation wiring (defer until bandit shadow on)

`scheduler.py:112` TODO — information-coefficient observation recording is unwired.
Only build when `BANDIT_WEIGHTING_SHADOW=true`. No action until then.

### P2.4 `api/deps.py` observability (background-review finding) ✅ DONE

`get_current_user` now bumps a process-global `_auth_failures` counter on every 401 and
emits a rate-limited (once/60s) WARNING summary, so a burst is visible above the DEBUG
per-request logs without a metrics backend. `# ponytail:` swap for Prometheus/StatsD if one
lands. Test: `test_auth.py::test_401_increments_auth_failure_counter`.

---

## P3.2 — Options Depth

**Gate:** start only after ~2–4 weeks of wheel paper data (from P3.1). Measure before building.
Watch `options_positions` transitions, collateral vs 15% cap, assignment reconciliation logs.

**Delta-band strike selection.** Currently strike picking is naive. Alpaca options snapshots
include greeks.
- Extend the contract picker in `trader/execution/options_broker.py` to target ~0.20–0.30 delta
  with a premium-yield floor; reject contracts outside the band.
- Test: synthetic chain with known greeks → picker returns the in-band strike; empty band → no entry.

**Roll logic (wires the "dead" methods).**
- CSP: when short put is ITM near expiry, roll out (same strike, later expiry) via `buy_to_close`
  + new sell — instead of passive assignment.
- Covered call: roll up-and-out when strike breached.
- Test: ITM-near-expiry state → roll orders emitted; OTM → passive hold.

**Early-assignment alert.** At reconcile time, `send_alert` when a short put is deep ITM near
ex-dividend (reuse `reconcile_options` + `alerts.py`).
- Test: deep-ITM + upcoming ex-div fixture → one alert.

**Multi-contract sizing.** Currently hardcoded 1 contract/entry (`options_broker.py:204`).
- Size by collateral vs cap once the cap is raised. Gate behind the cap-raise decision.

**Optional cap raise:** `MAX_OPTIONS_ALLOCATION_PCT` 0.15 → higher (env change) after the wheel
proves out. User decision, not code.

---

## P4 — Real-Money Go-Live (Robinhood)

**Gate:** only after the paper track record (equity + options) is demonstrably profitable.
See memory `project_robinhood_live_plan.md`. Alpaca stays for paper.

- **Broker abstraction:** route live orders through Robinhood MCP (`mcp__robinhood-trading__*`)
  behind the existing broker interface; keep Alpaca as the paper implementation. **Never
  auto-execute** — every real-money order placement stays a human-confirmed action.
- **Live cost model:** apply the real Robinhood/crypto fee + sizing haircut deferred in P1.4.
- **Destructive migrations:** now safe to drop the unused `trades` table (P2.2 kept it).
- **Kill switch + daily-loss halt:** re-validate both against the live broker path before first
  real order.

**Verify:** dry-run reconciliation against a funded-but-idle Robinhood account; confirm positions
read back correctly and no order is placed without explicit human confirmation.
