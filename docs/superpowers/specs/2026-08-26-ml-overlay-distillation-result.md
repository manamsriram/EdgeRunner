# ML-Overlay Phase 2: Distillation Feasibility — Result

**Date:** 2026-08-26
**Status:** Gate FAILED — full replacement not supported by the data. Enriched logging
shipped instead; retry once text features accumulate.
**Predecessors:** `2026-07-13-llm-overlay-cost-measurement.md` (Phase 0),
`2026-07-13-ml-overlay-phase1-feature-logging.md` (Phase 1)

## Question

Can a model trained on the logged feature vector reproduce the LLM overlay's
approve/veto decision closely enough to replace it? Gate: holdout ROC-AUC ≥ 0.75 **and**
LLM agreement ≥ 0.85.

Scope was buys only. The overlay also vetoes sells (1,515 rows, 29% veto vs 48% for
buys), `side` is absent from the feature vector, and a wrong sell-veto strands a losing
position — so sells were excluded and keep calling the LLM.

## Result: FAIL

Rolling-origin, 4 folds, deduplicated (3,774 distinct buy decisions, 2026-07-29 → 08-26):

| model | ROC-AUC | LLM agreement |
|---|---|---|
| Logistic regression | 0.757 ± 0.099 | 0.725 ± 0.081 |
| HistGradientBoosting | 0.752 ± 0.097 | 0.726 ± 0.090 |
| Majority baseline | — | 0.619 |
| **Gate** | **≥ 0.75** | **≥ 0.85** |

AUC scrapes its bar with ±0.10 variance; agreement misses badly. The model learns
something real (0.73 vs 0.62) but is not a replacement.

## Why the plan's GBDT escalation is disproven, not deferred

Phase 1's plan said agreement < 0.85 justifies escalating to gradient boosting. On raw
rows GBDT appeared to clear the gate outright — 0.943 AUC, 0.875 agreement. **That was
an artifact.**

The scheduler re-evaluates every symbol each tick, but the feature vector only moves on
daily bar/news refreshes, and `apply_claude_overlay` caches decisions for 30 minutes
keyed by `(symbol, side)`. One symbol therefore emits dozens of byte-identical rows per
day carrying a *replayed* decision. Measured: 16,757 raw rows → **3,774 distinct
decisions** (4.4×), and 21,040 cache hits vs 11,121 genuine LLM calls in `llm_call_log`.

GBDT was memorising duplicates that appeared on both sides of the split. Once collapsed,
its advantage over logistic regression is **zero**. Two model classes of very different
capacity scoring identically is the signature of an information limit, not a capacity
limit — so more model is not the answer.

## Why it's an information limit

The LLM is **96.2% self-consistent** on identical feature vectors (113 conflicting groups
of 893 repeated-input groups). So the 0.85 gate was never blocked by LLM randomness —
we sit at 0.73 against a reachable ~0.96 ceiling.

Comparing `claude_overlay.py`'s prompt against `features.py`, the prompt carries three
things the feature vector does not:

- **Full news headline text.** Features hold only per-category counts.
- **The strategy's free-text `signal.reason`.**
- The LLM's own **rationale** (output, but useful for analysis).

That is where the missing ~23% lives.

## Also found

- **`order_id` is a contaminated label.** Of ~8,000 LLM approvals only 756 became
  orders; the risk gate rejected the rest downstream. Training on it learns the risk
  gate. The correct label is the `signals.reason` prefix — which also corrects Phase 1's
  recorded limitation that post-overlay output "is NOT captured". It is, in `signals`.
- **`regime` is constant.** All rows are `'normal'`; the three one-hot columns carry zero
  information. `classify_regime` has emitted nothing else in 30 days — a separate bug.
- **The no-history sentinel is the majority case.** `days_since_last_trade = -1.0` in 85%
  of rows, with `last_trade_pnl_pct` / `win_rate_last_3` at 0.0 alongside it, where 0.0
  is also legitimate. Handled by a derived `has_trade_history` flag.
- **Strength distillation isn't worth building.** The LLM leaves strength untouched 30%
  of the time and moves it 0.055 on average; it only feeds ranking priority.

## Confidence-banded deferral (measured, not shipped)

| model handles | agreement on its share |
|---|---|
| 21.6% | 95.9% |
| 29.9% | 91.2% |
| 39.5% | 87.4% |

At the top band the model matches the LLM about as well as the LLM matches itself
(95.9% vs 96.2%). Not pursued for now — the decision was to fix the features first
rather than ship a partial replacement.

## What shipped instead

- `overlay_prompts` + `overlay_decisions` (migration 011): the exact prompt text and the
  parsed LLM answer, per genuine decision. Prompt text is stored once per hash; cache
  replays are not recorded at all, so every row is an independent sample.
- `trader/ml_overlay/transform.py`: preprocessing shared by the dataset builder and
  (future) inference, so training and serving cannot drift.
- `scripts/build_ml_dataset.py` (with deduplication) and `scripts/train_overlay_model.py`.
- `requirements-ml.txt`: sklearn is training-time only. Runtime inference stays numpy-only
  because the trading process has a documented OOM history.

## Retry criteria

Text features accumulate at ~190 distinct buy decisions/day. Revisit at ~8,000 rows
(≈2 months, so around **2026-10-25**) using headline-text embeddings computed offline.
Re-run the same gate. If agreement still stalls near 0.73 with text included, the
overlay's decision is not reproducible from what we can log, and confidence-banded
deferral becomes the fallback worth shipping.
