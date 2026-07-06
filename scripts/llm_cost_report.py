"""LLM cost report: summarise llm_call_log to measure actual overlay/gate spend.

Phase 0 of the ML-overlay research plan (~/.claude/plans/groovy-riding-goose.md) —
run this after 1-2 weeks of live instrumentation before deciding whether replacing
the LLM overlay with a trained model is worth the engineering below.

Usage:
    python scripts/llm_cost_report.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> int:
    try:
        from trader.config import load_config
        config = load_config()
    except Exception as exc:
        print(f"ERROR: could not load config — {exc}")
        return 2

    if not config.database_url:
        print("ERROR: DATABASE_URL not set — cannot generate report")
        return 2

    from trader.portfolio.postgres_repo import PostgresRepository
    repo = PostgresRepository(config.database_url)

    with repo._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM llm_call_log ORDER BY ts")
            rows = [dict(r) for r in cur.fetchall()]

    print("=" * 60)
    print("LLM Cost Report")
    print("=" * 60)

    if not rows:
        print("No llm_call_log rows yet — instrumentation may not have run, "
              "or not enough time has passed. Nothing to report.")
        return 1

    total_calls = len(rows)
    cache_hits = [r for r in rows if r["cache_hit"]]
    live_calls = [r for r in rows if not r["cache_hit"]]
    total_cost = sum(r["est_cost_usd"] for r in rows)

    print(f"Date range          : {rows[0]['ts'][:10]} → {rows[-1]['ts'][:10]}")
    print(f"Total calls          : {total_calls}")
    print(f"  cache hits         : {len(cache_hits)} ({len(cache_hits) / total_calls:.0%})")
    print(f"  live LLM calls     : {len(live_calls)}")
    print(f"Total est. cost      : ${total_cost:.4f}")

    days = _distinct_days(rows)
    if days:
        print(f"Days spanned         : {days}")
        print(f"Avg $/day            : ${total_cost / days:.4f}")

    print("\nBy call site:")
    for site in sorted({r["call_site"] for r in rows}):
        site_rows = [r for r in rows if r["call_site"] == site]
        site_calls = len(site_rows)
        site_hits = sum(1 for r in site_rows if r["cache_hit"])
        site_cost = sum(r["est_cost_usd"] for r in site_rows)
        print(f"  {site:20s} calls={site_calls:5d}  cache_hit_rate={site_hits / site_calls:.0%}  cost=${site_cost:.4f}")

    print("\nBy provider (live calls only):")
    for provider in sorted({r["provider"] for r in live_calls}):
        prov_rows = [r for r in live_calls if r["provider"] == provider]
        prov_calls = len(prov_rows)
        prov_cost = sum(r["est_cost_usd"] for r in prov_rows)
        print(f"  {provider:20s} calls={prov_calls:5d}  cost=${prov_cost:.4f}")

    print("\n" + "=" * 60)
    print("Compare $/day above against Render+Supabase hosting cost and the")
    print("account's expected daily P&L (docs/return-plan.md) before deciding")
    print("whether Phase 1+ of the ML-overlay plan is worth building.")
    return 0


def _distinct_days(rows: list[dict]) -> int:
    return len({r["ts"][:10] for r in rows})


if __name__ == "__main__":
    sys.exit(main())
