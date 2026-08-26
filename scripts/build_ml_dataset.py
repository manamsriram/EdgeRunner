"""Build the labelled training set for the ML overlay (distillation of the LLM).

Joins decision_features (the point-in-time feature snapshot) to signals (which holds
the post-overlay decision) and emits one CSV row per LLM buy decision.

Label source: `signals.reason`, which apply_overlay writes as
"[overlay veto] ..." / "[overlay approved] ..." immediately after the LLM call.
Deliberately NOT `decision_features.order_id` — only ~9% of LLM approvals become
orders because the risk gate rejects the rest downstream in _execute_signal, so a
model trained on order_id would learn the risk gate instead of the LLM.

Once the ML overlay governs live it writes a distinct "[ml overlay ...]" prefix, so
rows decided by the model are excluded here automatically and a retrain never learns
from the model's own output.

Usage:
    python scripts/build_ml_dataset.py [--out DIR]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Rows whose reason carries neither prefix (overlay skipped, e.g. no API key
# configured that tick) have no LLM decision to learn from and are dropped.
_QUERY = """
SELECT d.ts,
       d.symbol,
       d.strategy,
       d.features,
       CASE WHEN s.reason LIKE '[overlay veto]%%' THEN 1 ELSE 0 END AS label_veto
  FROM decision_features d
  JOIN signals s
    ON s.run_id = d.run_id
   AND s.symbol = d.symbol
 WHERE d.side = 'buy'
   AND d.mode = 'auto'
   AND (s.reason LIKE '[overlay veto]%%' OR s.reason LIKE '[overlay approved]%%')
 ORDER BY d.ts
"""

# Nothing in the schema stops a second signals row landing on the same (run_id,
# symbol); today there are zero, but a silent fan-out would duplicate training rows
# and inflate every metric, so check rather than assume.
_FANOUT_QUERY = """
SELECT COUNT(*) AS c FROM (
    SELECT run_id, symbol FROM signals GROUP BY run_id, symbol HAVING COUNT(*) > 1
) t
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/ml_overlay")
    args = parser.parse_args()

    from trader.config import load_config
    config = load_config()
    if not config.database_url:
        print("ERROR: DATABASE_URL not set")
        return 2

    from trader.portfolio.postgres_repo import PostgresRepository

    repo = PostgresRepository(config.database_url)
    with repo._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_FANOUT_QUERY)
            fanout = cur.fetchone()["c"]
            if fanout:
                print(f"ERROR: {fanout} (run_id, symbol) pairs have multiple signals "
                      "rows — the join would duplicate training rows. Fix the query "
                      "before trusting any metric from this dataset.")
                return 2

            cur.execute(_QUERY)
            rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        print("ERROR: no labelled rows — has the overlay run since feature logging shipped?")
        return 1

    records = []
    for r in rows:
        raw = r["features"]
        if isinstance(raw, str):          # SQLite/TEXT column; JSONB arrives parsed
            raw = json.loads(raw)
        # Raw, exactly as logged. transform.derive is applied at train time and again
        # at predict time — keeping it out of the CSV means both paths run the same
        # single implementation over the same input shape.
        rec = {k: float(v) for k, v in raw.items()}
        rec["label_veto"] = r["label_veto"]
        rec["ts"] = r["ts"]
        rec["symbol"] = r["symbol"]
        rec["strategy"] = r["strategy"]
        records.append(rec)

    # A feature missing from any row would silently become an empty CSV cell and then
    # a NaN at train time. Every row must carry the identical key set.
    keys = set(records[0])
    for i, rec in enumerate(records):
        if set(rec) != keys:
            print(f"ERROR: row {i} ({rec.get('symbol')}) has feature keys "
                  f"{sorted(set(rec) ^ keys)} differing from row 0")
            return 2

    feature_cols = sorted(k for k in keys if k not in ("label_veto", "ts", "symbol", "strategy"))
    columns = ["ts", "symbol", "strategy"] + feature_cols + ["label_veto"]

    # Collapse identical same-day decisions. The scheduler re-evaluates every symbol
    # each tick, but the feature vector only moves on daily bars/news refreshes, so one
    # symbol contributes dozens of byte-identical rows per day. Measured: 16,757 raw
    # rows collapse to 3,765 distinct decisions. Leaving them in silently reweights the
    # training set by polling frequency and — worse — puts copies of the same input in
    # both sides of an evaluation split, which is what made gradient boosting look far
    # better than it is (0.94 AUC on duplicated rows, 0.75 once collapsed).
    # Label is the majority vote; the LLM is ~96% self-consistent on identical inputs.
    deduped: dict[tuple, dict] = {}
    for rec in records:
        k = (rec["ts"][:10], rec["symbol"]) + tuple(rec[c] for c in feature_cols)
        slot = deduped.setdefault(k, {"rec": rec, "votes": []})
        slot["votes"].append(rec["label_veto"])

    collapsed = []
    for slot in deduped.values():
        rec = dict(slot["rec"])
        rec["label_veto"] = int(sum(slot["votes"]) * 2 >= len(slot["votes"]))
        collapsed.append(rec)
    collapsed.sort(key=lambda r: r["ts"])

    print(f"deduplicated: {len(records)} raw rows -> {len(collapsed)} distinct decisions")
    records = collapsed

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"dataset_{date.today().isoformat()}.csv")
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)

    vetoes = sum(r["label_veto"] for r in records)
    print(f"wrote {path}")
    print(f"  rows      : {len(records)}")
    print(f"  features  : {len(feature_cols)}")
    print(f"  veto      : {vetoes} ({100.0 * vetoes / len(records):.1f}%)")
    print(f"  approve   : {len(records) - vetoes}")
    print(f"  date range: {records[0]['ts'][:10]} -> {records[-1]['ts'][:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
