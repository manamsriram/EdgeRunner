# ML-Overlay Phase 1: Feature-Snapshot Logging — Design

**Date:** 2026-07-13
**Status:** Shipped — logging live, dataset accumulating

## Problem

Phase 2 training needs a labeled dataset. No historical feature snapshot exists
at decision time today — only regime bucket + signal.strength + a free-text
rationale are stored. Point-in-time news/sentiment/fundamentals cannot be
backfilled (free-tier APIs don't support "as of" queries), so the dataset can
only start accumulating from when this logging ships.

## What ships

- `decision_features` table (migration 008): one row per overlay decision,
  full numeric feature vector in `features` (JSONB/TEXT), linked to the
  resulting order via `order_id` (NULL for holds/vetoes — expected, not a bug).
- `build_feature_vector` (`trader/ml_overlay/features.py`): quantitative
  (price/vol from `market_stats.py`, shared with the LLM prompt builder so the
  two never drift) + qualitative (news category counts, sentiment ratio,
  parsed fundamentals) + trade-memory + one-hot regime.
- Wired into `trader/pipeline.py::_prepare_signal` right before `apply_overlay`,
  and `order_id` back-filled in `_execute_signal` right after `record_order`.

## Known limitations (accepted, not solved)

- **Survivorship bias**: risk-gate rejections are never persisted
  (`trader/risk/gate.py` is a pure stateless function) — the dataset is biased
  toward already-approved trades. The LLM has the same blind spot today.
- **Manual-mode deferral bias**: `mode='manual'` decision_features rows have
  `order_id=NULL` forever for a DIFFERENT reason than vetoes do — the human
  just deferred the trade, not vetoed it. Phase 2 trainers MUST filter by
  `mode='auto'` before treating NULL-`order_id` rows as negative labels;
  otherwise the dataset confuses "human said maybe" with "model said no".
- **Options orders (CSP/Wheel)**: are linked to `decision_features.order_id`
  by `_execute_csp_entry` (Step 4b) — but the link is to the option CONTRACT
  symbol's `orders.id`, not the underlying's `trade_outcomes` row. Phase 2
  trainers that want to score CSP/Wheel performance need a strategy-specific
  join (contract_symbol → options_positions.opening_order_id → orders.id).
- **No historical backfill for qualitative features**: news/sentiment/
  fundamentals as of a past decision can't be reconstructed. Only
  quantitative price/vol features could theoretically be backfilled from
  stored OHLCV bars, and this ships did NOT do that backfill — every row is
  logged live going forward, `backfilled` column exists but is always `False`
  for now.
- **Post-overlay LLM output is NOT captured on the same row**: the original
  draft of `decision_features` had `llm_action` / `llm_strength_post` /
  `llm_rationale` columns. Those were DELIBERATELY dropped to keep Phase 1
  additive (capturing them requires either changing `apply_overlay`'s public
  signature OR re-running the LLM overlay in `_log_decision_features`, which
  doubles LLM cost). Equivalent signal lives in `SignalRow.reason` as
  `[overlay approved] <rationale>` or `[overlay veto] <rationale>` plain
  text — Phase 2 can parse it if needed. Re-add the columns in a Phase 2
  migration if the regex-parsing fallback isn't good enough.
- **External call volume is bounded, not eliminated**: `_log_decision_features`
  re-fetches news/fundamentals through the same Finnhub client singletons the
  overlay uses. A 60-second TTL cache — placed inside the shared
  `news_context.py::_fetch_finnhub_articles_classified` and
  `fundamental_gate.py::fetch_fundamentals_raw` functions themselves, so both
  the LLM overlay's own call path and `_log_decision_features` share the same
  cache by construction — absorbs same-tick doubles between the two callers
  to a worst-case of ~1 call per (symbol, endpoint) per minute. A tick that
  touches many fresh symbols will still issue up to N external calls at most
  once per minute per symbol. Full single-fetch would thread pre-fetched
  objects through `apply_overlay`'s signature — deferred since it's not
  additive. Revisit if `(symbol, minute) → Finnhub 429s` ever appear in logs.

## Minimum-data gate before Phase 2

Per the plan: do not start Phase 2 training until ≥500 linked (order_id set)
`decision_features` rows exist, with ≥30 losing-trade outcomes among them.
`trader.learning.link_outcomes.count_linked_decision_features(repo)` reports
the first number; the loss-outcome count needs an `orders.id <-> trade_outcomes`
join that doesn't exist yet — that join, plus the full walk-forward training
loop, is Phase 2 scope.

## Duration

Expect months, not weeks, given ~5-20 trades/month/strategy (per the plan).
Check `count_linked_decision_features` periodically; do not start Phase 2 early.
