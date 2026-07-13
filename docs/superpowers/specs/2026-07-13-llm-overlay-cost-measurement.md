# LLM Overlay Cost Measurement — Phase 0 Result

**Date:** 2026-07-13
**Status:** Measured — cost is noise, proceeding to Phase 1 anyway (see Decision)
**Plan:** `~/.claude/plans/groovy-riding-goose.md` ("Replace LLM overlay with a
trained, continuously-learning ML model")

## Problem

Before building an ML replacement for the LLM overlay (`trader/overlay/`),
Phase 0 measures whether the recurring LLM cost is actually a problem worth
solving.

## Measurement

Instrumentation shipped 2026-07-06 (`9b36234`): `call_llm` returns real
provider-reported token usage; every call + cache hit logged to
`llm_call_log` (migration 004, applied to prod).

`scripts/llm_cost_report.py` run 2026-07-13 against prod, 7 days of live data
(2026-07-06 → 2026-07-12):

```
Total calls          : 3789
  cache hits         : 917 (24%)
  live LLM calls     : 2872
Total est. cost      : $0.2560
Days spanned         : 7
Avg $/day            : $0.0366

By provider (live calls only):
  gemini    calls=2870  cost=$0.2560
  groq      calls=   2  cost=$0.0001
```

## Comparison against anchors

- **Hosting cost** (Render + Supabase, fixed monthly floor): several
  dollars/day even at minimum tiers — LLM cost is a small fraction of this.
- **Expected daily P&L** (`docs/return-plan.md`, ~15–40%/yr on $25k+ equity):
  roughly $10–30/day expected — $0.0366/day is ~0.1–0.4% of that.

**By the letter of the Phase 0 gate: this is noise.** Neither anchor makes
$0.037/day a real problem.

## Decision

Proceeding to Phase 1 anyway. Rationale: the goal is **zero recurring cost if
the ML overlay performs at parity**, not cost reduction from a large number —
even noise-level spend compounds indefinitely and has no ceiling on provider
pricing/behavior changes, unlike a fixed engineering cost paid once. This is
an explicit deviation from the plan's Phase 0 gate as originally written;
recording it here per the "win or lose" documentation discipline
(`groovy-riding-goose.md` guiding constraints).

All Phase 3 decision gates (must match/beat LLM live decisions AND beat a
no-overlay baseline in backtest) still apply before any Phase 4 cutover is
even discussed — this deviation only affects whether Phase 1 starts, not the
bar for shipping.
