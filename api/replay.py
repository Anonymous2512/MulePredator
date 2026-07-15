#!/usr/bin/env python3
"""
replay.py -- stream the generated dataset at the /score API to simulate a
live UPI transaction feed for the demo.

This is what makes the dashboard "come alive" without a real payment rail:
it reads the feature table in timestamp order and POSTs each transaction to
the running API at a controlled rate.

Run (with the API already running on :8000):
    pip install requests pandas
    python3 replay.py --features data/features/features.csv --rate 50

--rate is transactions per second (0 = as fast as possible).
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd
import requests


def _bool(v: object, default: bool = False) -> bool:
    if pd.isna(v):
        return default
    return bool(v)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, default=Path("data/features/features.csv"))
    parser.add_argument("--url", default="http://localhost:8000/score")
    parser.add_argument("--rate", type=float, default=50.0, help="txns/sec, 0 = unlimited")
    parser.add_argument("--limit", type=int, default=0, help="max txns to send, 0 = all")
    parser.add_argument("--alerts-only", action="store_true", help="only print responses that alerted")
    args = parser.parse_args()

    df = pd.read_csv(args.features, low_memory=False).sort_values("timestamp")
    if args.limit:
        df = df.head(args.limit)
    print(f"Replaying {len(df):,} transactions to {args.url} at {args.rate or 'unlimited'} txn/s ...")

    delay = 1.0 / args.rate if args.rate > 0 else 0.0
    sent = alerts = 0
    t0 = time.perf_counter()
    for r in df.itertuples(index=False):
        payload = {
            "txn_id": str(r.txn_id),
            "timestamp": str(r.timestamp),
            "account_id_from": str(r.account_id_from),
            "account_id_to": str(r.account_id_to),
            "amount_inr": float(r.amount_inr),
            "is_new_device": _bool(getattr(r, "auth_is_new_device", False)),
            "impossible_travel": _bool(getattr(r, "auth_impossible_travel", False)),
            "tls_version": (r.crypto_tls_version if not pd.isna(r.crypto_tls_version) else "TLSv1.3"),
            "key_size_bits": (int(r.crypto_key_size_bits) if not pd.isna(r.crypto_key_size_bits) else None),
            "is_forward_secret": _bool(getattr(r, "crypto_is_forward_secret", True), True),
            "hndl_risk": _bool(getattr(r, "crypto_hndl_risk", False)),
            "data_volume_mb": (float(r.crypto_data_volume_mb) if not pd.isna(r.crypto_data_volume_mb) else 0.0),
        }
        try:
            resp = requests.post(args.url, json=payload, timeout=5).json()
        except requests.RequestException as e:
            print(f"request failed: {e}")
            continue
        sent += 1
        if resp.get("alert"):
            alerts += 1
        if resp.get("alert") or not args.alerts_only:
            tag = f"[{resp.get('alert_tier', '?').upper()}]" if resp.get("alert") else ""
            print(f"{tag:16s} {resp.get('txn_id')}  score={resp.get('fraud_score')}  {resp.get('reason', '')[:90]}")
        if delay:
            time.sleep(delay)

    elapsed = time.perf_counter() - t0
    print(f"\nSent {sent:,} txns in {elapsed:.1f}s ({sent/elapsed:.0f}/s); {alerts} alerts.")


if __name__ == "__main__":
    main()
