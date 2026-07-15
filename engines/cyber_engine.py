#!/usr/bin/env python3
"""
cyber_engine.py

Per-transaction cyber anomaly scoring from the auth/telemetry features in
features.csv. Two layers:

  1. Calibrated rule-based flags (failed logins, new device, impossible
     travel, device/IP churn). Thresholds below were checked against real
     lift, not assumed -- see conversation history. On this synthetic
     dataset several of these rules are near-perfectly separating (e.g.
     sender_trailing_failed_logins >= 1 has 100% fraud precision), because
     the ATO injector's failed-login pattern essentially never occurs in
     legitimate baseline data. Being honest about this: real login friction
     (typos, forgotten passwords) would create some false positives that
     this synthetic data doesn't model, so treat the rule precision here as
     an upper bound, not a real-world expectation.

  2. IsolationForest (unsupervised, trained on the train split only) over
     the full set of trailing cyber features, to catch softer anomalies
     that don't trip a hard rule -- this is the part meant to generalize
     beyond the clean synthetic patterns above.

Usage:
    python3 cyber_engine.py --features data/features/features.csv --output-dir data/cyber
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

# Calibrated against real lift AFTER the leakage fix (generate.py now injects
# benign new-device/failed-login/travel events into legit accounts). With that
# noise present, a SINGLE occurrence is mostly benign (6-8x lift) but 2+ in one
# window is a strong signal (133-144x): legit users rarely fail twice or switch
# devices twice in 6h, but ATO/device-farm activity does. Thresholds set to
# where the real separation is, not at >=1 (which would now flag benign noise).
FAILED_LOGIN_THRESHOLD = 2
IMPOSSIBLE_TRAVEL_THRESHOLD = 2
NEW_DEVICE_THRESHOLD = 2
DEVICE_CHURN_THRESHOLD = 3
IP_CHURN_THRESHOLD = 3

ISO_FOREST_FEATURES = [
    "sender_trailing_login_count", "sender_trailing_failed_logins", "sender_trailing_new_device_count",
    "sender_trailing_impossible_travel_count", "sender_trailing_max_velocity",
    "sender_trailing_distinct_devices", "sender_trailing_distinct_ips",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--features", type=Path, default=Path("data/features/features.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/cyber"))
    parser.add_argument("--ground-truth-eval", action="store_true",
                         help="print a label_is_fraud lift report (evaluation only)")
    args = parser.parse_args()

    cols = [
        "txn_id", "account_id_from", "split",
        "auth_is_new_device", "auth_impossible_travel", "auth_velocity_kmph",
        "sender_trailing_login_count", "sender_trailing_failed_logins", "sender_trailing_new_device_count",
        "sender_trailing_impossible_travel_count", "sender_trailing_max_velocity",
        "sender_trailing_distinct_devices", "sender_trailing_distinct_ips",
    ]
    label_cols = ["label_is_fraud"] if args.ground_truth_eval else []
    print(f"Loading {args.features} ...")
    t0 = time.perf_counter()
    df = pd.read_csv(args.features, usecols=cols + label_cols, low_memory=False)
    print(f"  loaded {len(df):,} rows in {time.perf_counter() - t0:.1f}s")

    for c in ISO_FOREST_FEATURES:
        df[c] = df[c].fillna(0)

    # --- rule-based flags ---
    df["flag_failed_login"] = df["sender_trailing_failed_logins"] >= FAILED_LOGIN_THRESHOLD
    df["flag_impossible_travel"] = (
        (df["sender_trailing_impossible_travel_count"] >= IMPOSSIBLE_TRAVEL_THRESHOLD)
        | df["auth_impossible_travel"].fillna(False)
    )
    df["flag_new_device"] = df["sender_trailing_new_device_count"] >= NEW_DEVICE_THRESHOLD
    df["flag_device_churn"] = df["sender_trailing_distinct_devices"] >= DEVICE_CHURN_THRESHOLD
    df["flag_ip_churn"] = df["sender_trailing_distinct_ips"] >= IP_CHURN_THRESHOLD
    # softer, partial-credit signals for the single-occurrence / 2-endpoint
    # range. After the leakage fix these still carry real but weak lift
    # (single failed login 8x, single new device 6.2x, 2 distinct ips 7.8x),
    # so they contribute partial score rather than a full flag -- the engine
    # uses them without treating any one as a fraud oracle.
    df["soft_churn_score"] = (
        ((df["sender_trailing_distinct_devices"] == 2).astype(float) * 0.25)
        + ((df["sender_trailing_distinct_ips"] == 2).astype(float) * 0.25)
        + ((df["sender_trailing_failed_logins"] == 1).astype(float) * 0.20)
        + ((df["sender_trailing_new_device_count"] == 1).astype(float) * 0.15)
    ).clip(upper=0.5)

    rule_score = (
        df["flag_failed_login"].astype(float) * 1.0
        + df["flag_impossible_travel"].astype(float) * 1.0
        + df["flag_new_device"].astype(float) * 0.6
        + df["flag_device_churn"].astype(float) * 1.0
        + df["flag_ip_churn"].astype(float) * 1.0
        + df["soft_churn_score"]
    ).clip(upper=1.0)

    # --- IsolationForest on the train split only ---
    print("Training IsolationForest on the train split ...")
    t0 = time.perf_counter()
    train_mask = df["split"] == "train"
    iso = IsolationForest(n_estimators=200, contamination=0.02, random_state=42, n_jobs=-1)
    iso.fit(df.loc[train_mask, ISO_FOREST_FEATURES])
    raw_scores = -iso.score_samples(df[ISO_FOREST_FEATURES])  # higher = more anomalous
    # NOTE: min-max clipping to the [1st,99th] percentile range was tried first
    # and created a large artificial plateau -- ~1% of 2M+ rows (~20k) all
    # clipped to the exact ceiling value, which is a lot of rows to treat as
    # equally maximally anomalous. Switched to the same calibrated rank-lookup
    # approach used for hub_score in graph_engine.py, checked against real
    # lift: near-baseline until the 99th percentile, then rising sharply
    # (12.6x at 0.99, 103.3x at 0.999).
    iso_percentile = pd.Series(raw_scores, index=df.index).rank(pct=True)
    iso_bucket_edges = np.array([0, 0.90, 0.95, 0.98, 0.99, 0.995, 0.999, 1.0001])
    iso_bucket_score = np.array([0.02, 0.05, 0.10, 0.20, 0.35, 0.55, 1.00])
    iso_idx = np.clip(np.digitize(iso_percentile, iso_bucket_edges) - 1, 0, len(iso_bucket_score) - 1)
    iso_score = iso_bucket_score[iso_idx]
    print(f"  done in {time.perf_counter() - t0:.1f}s")

    df["cyber_iso_score"] = iso_score
    # rules dominate when they fire (they're near-deterministic here); isolation
    # forest contributes when no hard rule fired, to catch softer anomalies
    df["cyber_risk_score"] = np.maximum(rule_score, 0.5 * df["cyber_iso_score"])

    def _reason(row: pd.Series) -> str:
        parts = []
        if row["flag_failed_login"]:
            parts.append("failed login burst")
        if row["flag_impossible_travel"]:
            parts.append("impossible travel velocity")
        if row["flag_new_device"]:
            parts.append("new device")
        if row["flag_device_churn"] or row["flag_ip_churn"]:
            parts.append("device/IP churn")
        if not parts and row["cyber_iso_score"] > 0.7:
            parts.append("unusual auth pattern (isolation forest)")
        return "; ".join(parts) if parts else "no cyber signal"

    df["cyber_reason"] = df.apply(_reason, axis=1)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_cols = ["txn_id", "account_id_from", "cyber_risk_score", "cyber_iso_score", "cyber_reason",
                "flag_failed_login", "flag_impossible_travel", "flag_new_device", "flag_device_churn", "flag_ip_churn"]
    out_path = args.output_dir / "cyber_features.csv"
    df[out_cols].to_csv(out_path, index=False)
    print(f"\nWrote {len(df):,} rows to {out_path}")
    print(f"Transactions with cyber_risk_score > 0.5: {(df['cyber_risk_score'] > 0.5).sum():,} "
          f"({(df['cyber_risk_score'] > 0.5).mean():.3%})")

    if args.ground_truth_eval:
        baseline = df["label_is_fraud"].mean()
        print(f"\n--- Evaluation (label used only for reporting, not scoring) ---")
        print(f"baseline txn-level fraud rate: {baseline:.3%}")
        for thresh in [0.3, 0.5, 0.7, 0.9]:
            flagged = df[df["cyber_risk_score"] >= thresh]
            rate = flagged["label_is_fraud"].mean() if len(flagged) else 0
            recall = flagged["label_is_fraud"].sum() / max(df["label_is_fraud"].sum(), 1)
            print(f"cyber_risk_score>={thresh}: n={len(flagged):7,d}  fraud_rate={rate:.2%}  "
                  f"lift={rate / baseline:.1f}x  recall={recall:.1%}")


if __name__ == "__main__":
    main()
