#!/usr/bin/env python3
"""
build_feature_table.py

Turns the raw tables from generate.py into a single ML-ready feature table:
one row per transaction, scored from the SENDER's point of view, at the
moment that transaction happened, using only information available at or
before that timestamp (a trailing sliding window). This is the same trigger
a live "/score" endpoint would use -- a transaction arrives, and the engine
looks back over the account's recent history to decide how risky it is.

Feature groups, one row per transaction:
  - sender_*    : trailing transactional behavior (amounts, counts, fan-out,
                  in/out ratio) computed from the sender's own account timeline
  - receiver_*  : a small set of trailing behavior features for the receiving
                  account (fan-in, distinct senders) -- useful for spotting
                  collector/hub accounts (smurfing, hub-and-spoke)
  - auth_*      : the specific login event tied to this transaction, plus
                  trailing auth behavior (failed-login rate, new device,
                  distinct devices/IPs, impossible travel) for the sender
  - crypto_*    : this transaction's own session crypto posture, plus the
                  sender's trailing weak-crypto/non-PQC exposure rate
  - label_*     : ground truth, never to be used as a model input. is_fraud
                  is causally gated on compromise_timestamp so a mule
                  account's PRE-compromise transactions are correctly
                  labeled not-yet-fraud, which is what makes an early
                  detection lead-time metric meaningful.

Usage:
    python3 build_feature_table.py \
        --input-dir data/final \
        --output-dir data/features \
        --config config.yaml \
        --window 6h
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


def read_config(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)


def load_raw(input_dir: Path) -> dict[str, pd.DataFrame]:
    print(f"Loading raw tables from {input_dir} ...")
    t0 = time.perf_counter()
    accounts = pd.read_csv(input_dir / "accounts.csv")
    tx = pd.read_csv(
        input_dir / "transactions.csv",
        parse_dates=["timestamp"],
        dtype={"pattern_type": "string", "scenario_id": "string"},
        low_memory=False,
    )
    auth = pd.read_csv(
        input_dir / "auth_events.csv",
        parse_dates=["timestamp"],
        dtype={"pattern_type": "string", "scenario_id": "string"},
        low_memory=False,
    )
    crypto = pd.read_csv(input_dir / "crypto_sessions.csv", low_memory=False)
    ground_truth = pd.read_csv(input_dir / "ground_truth.csv", parse_dates=["compromise_timestamp"])
    print(f"  loaded in {time.perf_counter() - t0:.1f}s: "
          f"{len(accounts):,} accounts, {len(tx):,} txns, {len(auth):,} auth events, "
          f"{len(crypto):,} crypto sessions, {len(ground_truth):,} ground truth rows")
    return {"accounts": accounts, "tx": tx, "auth": auth, "crypto": crypto, "ground_truth": ground_truth}


def map_upi_to_account(tx: pd.DataFrame, accounts: pd.DataFrame) -> pd.DataFrame:
    dupe_upi = accounts["upi_id"].duplicated(keep=False)
    if dupe_upi.any():
        n_dupe_accounts = int(dupe_upi.sum())
        n_dupe_upis = int(accounts.loc[dupe_upi, "upi_id"].nunique())
        print(
            f"  NOTE: {n_dupe_accounts} accounts ({n_dupe_upis} distinct upi_id values) share a upi_id with "
            f"another account -- this is a known low-rate collision in generate.py's upi_id construction "
            f"(first.last+suffix@bank has occasional suffix collisions at 50k scale). Keeping the first "
            f"account for each duplicated upi_id; affected transactions will be attributed to that account. "
            f"Fix at the source by widening the suffix range in build_accounts if you want exact uniqueness."
        )
    upi_to_acct = accounts.drop_duplicates("upi_id", keep="first").set_index("upi_id")["account_id"]
    tx = tx.copy()
    tx["account_id_from"] = tx["upi_id_from"].map(upi_to_acct)
    tx["account_id_to"] = tx["upi_id_to"].map(upi_to_acct)
    missing = int(tx["account_id_from"].isna().sum() + tx["account_id_to"].isna().sum())
    if missing:
        print(f"  WARNING: {missing} transaction endpoints did not map to a known account_id -- dropping those rows")
        tx = tx.dropna(subset=["account_id_from", "account_id_to"])
    return tx


def build_txn_flow_features(tx: pd.DataFrame, window: str) -> pd.DataFrame:
    """One row per txn_id, with trailing sender (fan-out) and receiver
    (fan-in) behavior. See module docstring for the join strategy."""
    print(f"  building transaction flow features (window={window}) ...")
    t0 = time.perf_counter()

    out_leg = pd.DataFrame({
        "account_id": tx["account_id_from"].to_numpy(),
        "timestamp": tx["timestamp"].to_numpy(),
        "txn_id": tx["txn_id"].to_numpy(),
        "amount": tx["amount_inr"].to_numpy(),
        "counterparty": tx["account_id_to"].to_numpy(),
        "leg": "out",
    })
    in_leg = pd.DataFrame({
        "account_id": tx["account_id_to"].to_numpy(),
        "timestamp": tx["timestamp"].to_numpy(),
        "txn_id": tx["txn_id"].to_numpy(),
        "amount": tx["amount_inr"].to_numpy(),
        "counterparty": tx["account_id_from"].to_numpy(),
        "leg": "in",
    })

    def _rolling_leg_features(leg_df: pd.DataFrame, prefix: str) -> pd.DataFrame:
        leg_df = leg_df.sort_values(["account_id", "timestamp"]).reset_index(drop=True)
        leg_df["cp_code"] = pd.factorize(leg_df["counterparty"])[0].astype(float)
        grouped = leg_df.set_index("timestamp").groupby("account_id", sort=False)
        leg_df[f"{prefix}_trailing_amount"] = grouped["amount"].rolling(window).sum().to_numpy()
        leg_df[f"{prefix}_trailing_count"] = grouped["amount"].rolling(window).count().to_numpy()
        leg_df[f"{prefix}_trailing_max_amount"] = grouped["amount"].rolling(window).max().to_numpy()
        leg_df[f"{prefix}_trailing_distinct_counterparties"] = (
            grouped["cp_code"].rolling(window).apply(lambda x: np.unique(x).size, raw=True).to_numpy()
        )
        return leg_df.set_index("txn_id")[
            [f"{prefix}_trailing_amount", f"{prefix}_trailing_count",
             f"{prefix}_trailing_max_amount", f"{prefix}_trailing_distinct_counterparties"]
        ]

    sender_feats = _rolling_leg_features(out_leg, "sender")
    receiver_feats = _rolling_leg_features(in_leg, "receiver")
    result = sender_feats.join(receiver_feats, how="outer")
    result["sender_trailing_in_out_ratio"] = np.nan  # filled after merge with receiver's own inbound view below
    print(f"    done in {time.perf_counter() - t0:.1f}s")
    return result.reset_index()


def build_trailing_by_account(
    df: pd.DataFrame, account_col: str, ts_col: str, window: str, agg_specs: dict[str, tuple[str, str]]
) -> pd.DataFrame:
    """Generic trailing-window rolling aggregate, one row per input row,
    grouped by account_col. agg_specs maps output_col -> (source_col, how),
    where how in {"sum","count","mean","max","nunique"}."""
    d = df.sort_values([account_col, ts_col]).reset_index(drop=True)
    grouped = d.set_index(ts_col).groupby(account_col, sort=False)
    out = pd.DataFrame(index=d.index)
    for out_col, (src_col, how) in agg_specs.items():
        if how == "nunique":
            codes = pd.factorize(d[src_col])[0].astype(float)
            tmp = d[[account_col, ts_col]].copy()
            tmp["_code"] = codes
            g2 = tmp.set_index(ts_col).groupby(account_col, sort=False)
            out[out_col] = g2["_code"].rolling(window).apply(lambda x: np.unique(x).size, raw=True).to_numpy()
        else:
            out[out_col] = getattr(grouped[src_col].rolling(window), how)().to_numpy()
    out[account_col] = d[account_col].to_numpy()
    out[ts_col] = d[ts_col].to_numpy()
    for extra in ("_key_id",):
        if extra in d.columns:
            out[extra] = d[extra].to_numpy()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", type=Path, default=Path("data/final"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/features"))
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--window", type=str, default="6h", help="trailing window size, pandas offset string")
    parser.add_argument("--format", choices=["csv", "parquet"], default="csv")
    args = parser.parse_args()

    config = read_config(args.config)
    raw = load_raw(args.input_dir)
    accounts, tx, auth, crypto, ground_truth = (
        raw["accounts"], raw["tx"], raw["auth"], raw["crypto"], raw["ground_truth"]
    )

    tx = map_upi_to_account(tx, accounts)

    # --- 1. direct 1:1 attributes for the specific auth event and crypto
    # session tied to each transaction ---
    print("Attaching direct per-transaction auth and crypto attributes ...")
    auth_direct = auth[["auth_event_id", "is_new_device", "success", "velocity_kmph", "impossible_travel"]].rename(
        columns={"is_new_device": "auth_is_new_device", "success": "auth_success",
                 "velocity_kmph": "auth_velocity_kmph", "impossible_travel": "auth_impossible_travel"}
    )
    tx = tx.merge(auth_direct, on="auth_event_id", how="left")

    crypto_direct = crypto[["txn_id", "tls_version", "is_pqc_protected", "is_forward_secret",
                             "key_size_bits", "data_volume_mb", "hndl_risk"]].rename(
        columns={"tls_version": "crypto_tls_version", "is_pqc_protected": "crypto_is_pqc_protected",
                 "is_forward_secret": "crypto_is_forward_secret", "key_size_bits": "crypto_key_size_bits",
                 "data_volume_mb": "crypto_data_volume_mb", "hndl_risk": "crypto_hndl_risk"}
    )
    tx = tx.merge(crypto_direct, on="txn_id", how="left")

    # --- 2. trailing sender/receiver transaction behavior ---
    flow_feats = build_txn_flow_features(tx, args.window)
    tx = tx.merge(flow_feats, on="txn_id", how="left")
    tx["sender_trailing_in_out_ratio"] = tx["receiver_trailing_amount"].fillna(0.0) / (
        tx["sender_trailing_amount"].fillna(0.0) + tx["receiver_trailing_amount"].fillna(0.0) + 1e-9
    )
    # NOTE: receiver_trailing_amount here is being reused as a proxy the sender's
    # own trailing inbound total ONLY if account plays both roles; a fully
    # correct in/out ratio per account (not per side-of-txn) is computed via
    # the dedicated pass below.
    tx = tx.drop(columns=["sender_trailing_in_out_ratio"])
    sender_inbound = build_trailing_by_account(
        pd.DataFrame({
            "account_id": tx["account_id_to"], "timestamp": tx["timestamp"],
            "txn_id": tx["txn_id"], "amount": tx["amount_inr"],
        }).rename(columns={"account_id": "acct"}),
        account_col="acct", ts_col="timestamp", window=args.window,
        agg_specs={"acct_trailing_inbound_amount": ("amount", "sum")},
    )
    # re-key onto sender accounts: for each transaction, look up the SENDER's
    # own trailing inbound total as of this transaction's timestamp
    sender_inbound = sender_inbound.rename(columns={"acct": "account_id_from"}).sort_values("timestamp")
    tx_sorted = tx.sort_values("timestamp")
    tx_sorted = pd.merge_asof(
        tx_sorted, sender_inbound, on="timestamp", by="account_id_from", direction="backward"
    )
    tx = tx_sorted
    tx["sender_trailing_in_out_ratio"] = tx["acct_trailing_inbound_amount"].fillna(0.0) / (
        tx["sender_trailing_amount"].fillna(0.0) + tx["acct_trailing_inbound_amount"].fillna(0.0) + 1e-9
    )

    # --- 3. trailing auth behavior for the sender account ---
    print("Building trailing auth behavior features ...")
    t0 = time.perf_counter()
    auth_trail = build_trailing_by_account(
        auth.rename(columns={"account_id": "acct"}),
        account_col="acct", ts_col="timestamp", window=args.window,
        agg_specs={
            "sender_trailing_login_count": ("auth_event_id", "count"),
            "sender_trailing_failed_logins": ("success", "sum"),  # sum of False->0/True->1 inverted below
            "sender_trailing_new_device_count": ("is_new_device", "sum"),
            "sender_trailing_impossible_travel_count": ("impossible_travel", "sum"),
            "sender_trailing_max_velocity": ("velocity_kmph", "max"),
            "sender_trailing_distinct_devices": ("device_id", "nunique"),
            "sender_trailing_distinct_ips": ("ip_address", "nunique"),
        },
    ).rename(columns={"acct": "account_id_from"})
    auth_trail["sender_trailing_failed_logins"] = (
        auth_trail["sender_trailing_login_count"] - auth_trail["sender_trailing_failed_logins"]
    )  # success sums True=1; failed = count - successes
    auth_trail = auth_trail.sort_values("timestamp")
    tx = tx.sort_values("timestamp")
    tx = pd.merge_asof(tx, auth_trail, on="timestamp", by="account_id_from", direction="backward")
    print(f"  done in {time.perf_counter() - t0:.1f}s")

    # --- 4. trailing crypto exposure for the sender account ---
    print("Building trailing crypto exposure features ...")
    t0 = time.perf_counter()
    crypto_with_acct = crypto.merge(tx[["txn_id", "account_id_from", "timestamp"]], on="txn_id", how="left")
    crypto_with_acct["is_weak_crypto"] = (~crypto_with_acct["is_pqc_protected"].astype(bool)).astype(float)
    crypto_trail = build_trailing_by_account(
        crypto_with_acct.rename(columns={"account_id_from": "acct"}),
        account_col="acct", ts_col="timestamp", window=args.window,
        agg_specs={
            "sender_trailing_weak_crypto_rate": ("is_weak_crypto", "mean"),
            "sender_trailing_hndl_session_count": ("hndl_risk", "sum"),
            "sender_trailing_min_key_size": ("key_size_bits", "max"),  # max as a stable proxy; NaNs common for PQC rows
        },
    ).rename(columns={"acct": "account_id_from"})
    crypto_trail = crypto_trail.sort_values("timestamp")
    tx = tx.sort_values("timestamp")
    tx = pd.merge_asof(tx, crypto_trail, on="timestamp", by="account_id_from", direction="backward")
    print(f"  done in {time.perf_counter() - t0:.1f}s")

    # --- 5. labels (never fed as model inputs downstream) ---
    print("Attaching labels ...")
    gt = ground_truth.rename(columns={"account_id": "account_id_from"})
    money_laundering_types = {"sleeper_mule", "smurfing", "device_farm", "ato"}
    gt["_has_ml_type"] = gt["fraud_types"].fillna("").apply(
        lambda s: bool(set(s.split("|")) & money_laundering_types)
    )
    tx = tx.merge(
        gt[["account_id_from", "is_fraud", "fraud_types", "is_hard_negative", "compromise_timestamp",
            "cluster_id", "_has_ml_type"]],
        on="account_id_from", how="left",
    )
    tx["label_is_fraud"] = (
        tx["is_fraud"].fillna(False)
        & tx["_has_ml_type"].fillna(False)
        & (tx["timestamp"] >= tx["compromise_timestamp"])
    )
    tx["label_hndl_exposed"] = tx["crypto_hndl_risk"].fillna(False).astype(bool)
    tx["label_is_hard_negative"] = tx["is_hard_negative"].fillna(False)
    tx["label_fraud_types"] = tx["fraud_types"].fillna("")
    tx["label_cluster_id"] = tx["cluster_id"]
    tx = tx.drop(columns=["is_fraud", "fraud_types", "is_hard_negative", "compromise_timestamp",
                           "cluster_id", "_has_ml_type"])

    # --- 6. split assignment, reusing the generator's own day boundaries ---
    sim_start = pd.Timestamp(config["global"]["sim_start"])
    day = ((tx["timestamp"] - sim_start).dt.total_seconds() / 86400) + 1
    # NOTE: boundaries are continuous (day > train_hi, not day >= val_lo) to
    # match generate.py's own make_splits exactly -- using the "start day"
    # integers as inclusive lower bounds leaves a gap for the fractional
    # part of a day between e.g. 20.0 and 21.0, silently dropping rows into
    # an "unassigned" bucket.
    tr_lo, tr_hi = config["split"]["train_days"]
    va_lo, va_hi = config["split"]["val_days"]
    tx["split"] = np.select(
        [
            (day >= tr_lo) & (day <= tr_hi),
            (day > tr_hi) & (day <= va_hi),
            (day > va_hi),
        ],
        ["train", "val", "holdout"],
        default="unassigned",
    )

    # --- 7. write outputs ---
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.output_dir / f"features.{args.format}"
    print(f"Writing {len(tx):,} rows x {tx.shape[1]} cols to {out_path} ...")
    if args.format == "parquet":
        try:
            tx.to_parquet(out_path, index=False)
        except ImportError:
            print("  pyarrow/fastparquet not available, falling back to CSV")
            out_path = args.output_dir / "features.csv"
            tx.to_csv(out_path, index=False)
    else:
        tx.to_csv(out_path, index=False)

    sample_path = args.output_dir / "features_sample.csv"
    tx.sample(min(2000, len(tx)), random_state=42).to_csv(sample_path, index=False)

    # --- 8. summary ---
    print()
    print("Feature Table Build Summary")
    print(f"Rows: {len(tx):,}   Columns: {tx.shape[1]}")
    print(f"label_is_fraud positives: {int(tx['label_is_fraud'].sum()):,} ({tx['label_is_fraud'].mean():.3%})")
    print(f"label_hndl_exposed positives: {int(tx['label_hndl_exposed'].sum()):,} ({tx['label_hndl_exposed'].mean():.3%})")
    print(f"label_is_hard_negative positives: {int(tx['label_is_hard_negative'].sum()):,} ({tx['label_is_hard_negative'].mean():.3%})")
    print("Split counts:")
    print(tx["split"].value_counts().to_string())
    null_check = tx.isna().mean().sort_values(ascending=False)
    print("Top columns by null fraction:")
    print(null_check.head(8).to_string())


if __name__ == "__main__":
    main()
