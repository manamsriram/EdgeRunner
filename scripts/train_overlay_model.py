"""Train the LLM-distillation model for the ML overlay and export it as JSON.

Reads the CSV from build_ml_dataset.py, fits a logistic regression, and writes
trader/ml_overlay/model.json — plain coefficients so the trading process can do
inference in numpy alone (sklearn/scipy never ship to Render; that process has a
documented OOM history).

Also writes a holdout fixture next to the artifact. tests/test_ml_overlay_predict.py
replays it through predict.py and asserts agreement with sklearn's own predict_proba;
that parity check is the only thing standing between an export bug and a model that
silently makes different decisions in production than it did in training.

Usage:
    python scripts/train_overlay_model.py [--dataset PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ARTIFACT = os.path.join(os.path.dirname(__file__), "..", "trader", "ml_overlay", "model.json")
FIXTURE = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "ml_overlay_holdout.json")

CLIP_SIGMA = 5.0
HOLDOUT_FRAC = 0.20
META_COLS = ("ts", "symbol", "strategy", "label_veto")

# Gate from the plan: below either of these, logistic regression has failed to capture
# the LLM's decision function and gradient-boosted trees become justified.
MIN_AUC = 0.75
MIN_AGREEMENT = 0.85


def _latest_dataset() -> str:
    matches = sorted(glob.glob("data/ml_overlay/dataset_*.csv"))
    if not matches:
        raise SystemExit("no dataset found — run scripts/build_ml_dataset.py first")
    return matches[-1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="report metrics but do not write the artifact")
    args = parser.parse_args()

    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import confusion_matrix, roc_auc_score

    from trader.ml_overlay.transform import derive

    path = args.dataset or _latest_dataset()
    df = pd.read_csv(path)
    print(f"dataset: {path}  ({len(df)} rows)")

    # The CSV holds raw logged features; derive() is the same function predict.py runs
    # at inference, so training and serving cannot diverge on preprocessing.
    raw_cols = [c for c in df.columns if c not in META_COLS]
    derived = pd.DataFrame([derive(rec) for rec in df[raw_cols].to_dict("records")])
    feature_names = sorted(derived.columns)
    # Raw columns ride along (prefixed, so they can never be mistaken for features) —
    # the holdout fixture needs pre-transform rows to exercise predict.py's real path.
    df = pd.concat([
        df[list(META_COLS)],
        derived[feature_names],
        df[raw_cols].add_prefix("raw__"),
    ], axis=1)
    print(f"features: {len(raw_cols)} raw -> {len(feature_names)} after transform")

    # Time-ordered split. A random split would put near-duplicate rows on both sides —
    # the same symbols are re-evaluated ~500x/day — and report an AUC that does not
    # survive contact with tomorrow's data.
    df = df.sort_values("ts").reset_index(drop=True)
    split = int(len(df) * (1.0 - HOLDOUT_FRAC))
    train, test = df.iloc[:split], df.iloc[split:]
    print(f"split   : train {len(train)} (through {train['ts'].iloc[-1][:10]}) | "
          f"holdout {len(test)} (from {test['ts'].iloc[0][:10]})")

    X_train_raw = train[feature_names].to_numpy(dtype=float)
    X_test_raw = test[feature_names].to_numpy(dtype=float)
    y_train = train["label_veto"].to_numpy(dtype=int)
    y_test = test["label_veto"].to_numpy(dtype=int)

    # Scaler is fit on the training split only — computing it over the full dataset
    # leaks holdout distribution into training.
    mean = X_train_raw.mean(axis=0)
    std = X_train_raw.std(axis=0)
    std[std == 0.0] = 1.0        # constant column: centre it, never divide by zero

    def prep(X):
        return np.clip((X - mean) / std, -CLIP_SIGMA, CLIP_SIGMA)

    X_train, X_test = prep(X_train_raw), prep(X_test_raw)

    model = LogisticRegression(max_iter=2000)
    model.fit(X_train, y_train)

    probs = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, probs)

    # Threshold is chosen, not assumed. The two errors cost differently: approving a
    # buy the LLM would have vetoed puts money at risk, while a spurious veto only
    # costs a missed entry. Prefer the most-agreeing threshold that does not approve
    # more wrongly than it vetoes wrongly.
    best = None
    for t in np.arange(0.20, 0.81, 0.01):
        pred = (probs >= t).astype(int)
        tn, fp, fn, tp = confusion_matrix(y_test, pred, labels=[0, 1]).ravel()
        false_approve = fn / max(fn + tp, 1)     # LLM vetoed, model approved
        false_veto = fp / max(fp + tn, 1)        # LLM approved, model vetoed
        agreement = (pred == y_test).mean()
        cand = (agreement, -abs(t - 0.5), float(t), false_approve, false_veto)
        if false_approve <= false_veto and (best is None or cand > best):
            best = cand
    if best is None:                              # no threshold satisfies the asymmetry
        best = (float((probs >= 0.5).astype(int) == y_test).mean(), 0.0, 0.5, 0.0, 0.0)
        print("WARNING: no threshold met the false-approve <= false-veto constraint; "
              "falling back to 0.50")
    agreement, _, threshold, false_approve, false_veto = best

    pred = (probs >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_test, pred, labels=[0, 1]).ravel()

    print("\n--- holdout ---")
    print(f"ROC-AUC        : {auc:.4f}      (gate >= {MIN_AUC})")
    print(f"threshold      : {threshold:.2f}")
    print(f"LLM agreement  : {agreement:.4f}      (gate >= {MIN_AGREEMENT})")
    print(f"false-approve  : {false_approve:.4f}  (LLM vetoed, model approved — the risky one)")
    print(f"false-veto     : {false_veto:.4f}  (LLM approved, model vetoed — a missed entry)")
    print(f"confusion      : tn={tn} fp={fp} fn={fn} tp={tp}")

    print("\n--- baselines (holdout) ---")
    print(f"always-approve : {(y_test == 0).mean():.4f}")
    print(f"always-veto    : {(y_test == 1).mean():.4f}")

    print("\n--- per strategy (holdout) ---")
    for name, grp in test.groupby("strategy"):
        idx = test.index.get_indexer(grp.index)
        print(f"{name:20s} n={len(grp):5d}  agreement={(pred[idx] == y_test[idx]).mean():.4f}  "
              f"veto_rate_llm={y_test[idx].mean():.3f}")

    print("\n--- coefficients (standardised, |value| desc) ---")
    for name, coef in sorted(zip(feature_names, model.coef_[0]), key=lambda kv: -abs(kv[1])):
        print(f"  {name:28s} {coef:+.4f}")

    passed = auc >= MIN_AUC and agreement >= MIN_AGREEMENT
    print(f"\nGATE: {'PASS' if passed else 'FAIL'} "
          f"(auc {auc:.4f} vs {MIN_AUC}, agreement {agreement:.4f} vs {MIN_AGREEMENT})")
    if not passed:
        print("Logistic regression did not capture the LLM's decision function. "
              "Per the plan this is what justifies escalating to HistGradientBoostingClassifier.")

    if args.dry_run:
        print("\n--dry-run: artifact not written")
        return 0 if passed else 1

    artifact = {
        "version": 1,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "dataset": os.path.basename(path),
        "n_train": len(train),
        "n_holdout": len(test),
        "feature_names": feature_names,
        "mean": mean.tolist(),
        "std": std.tolist(),
        "clip_sigma": CLIP_SIGMA,
        "coef": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "threshold": float(threshold),
        "holdout_metrics": {
            "roc_auc": float(auc),
            "agreement": float(agreement),
            "false_approve_rate": float(false_approve),
            "false_veto_rate": float(false_veto),
            "confusion": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
        },
    }
    os.makedirs(os.path.dirname(ARTIFACT), exist_ok=True)
    with open(ARTIFACT, "w") as fh:
        json.dump(artifact, fh, indent=2)
    print(f"\nwrote {os.path.normpath(ARTIFACT)}")

    # Raw (pre-transform) rows + sklearn's own probabilities, so the test can drive
    # predict.py end to end and catch any divergence in the numpy reimplementation.
    sample = test.head(200)
    fixture = {
        "rows": [
            {"features": {c[len("raw__"):]: float(row[c]) for c in sample.columns
                          if c.startswith("raw__")},
             "expected_prob": float(p)}
            for (_, row), p in zip(sample.iterrows(), probs[:len(sample)])
        ]
    }
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, "w") as fh:
        json.dump(fixture, fh)
    print(f"wrote {os.path.normpath(FIXTURE)} ({len(fixture['rows'])} rows)")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
