#!/usr/bin/env python3
"""
quantum_engine.py

Per-transaction harvest-now-decrypt-later (HNDL) exposure scoring.

Framing note, important for Q&A: this does NOT detect a quantum attack in
progress -- no quantum computer today can break current crypto, so there is
nothing to "detect" yet. What this scores is EXPOSURE: sessions using
crypto that will be breakable once quantum computers mature, weighted by
how much value would be worth harvesting now to decrypt later. That's a
risk/posture score, not an attack-detection score, and the two should not
be conflated in the pitch.

crypto_hndl_risk (generate.py's own flag: NOT forward_secret AND NOT pqc)
is already a deterministic per-session boolean. This engine's job is to
turn that binary into something a bank could actually prioritize against:

  - severity-weighted by exactly HOW weak the crypto is (TLS 1.0 with a
    1024-bit RSA key exchange is worse than TLS 1.2 with 2048-bit), not
    just weak/not-weak
  - weighted by data_volume_mb, since a small transfer is a low-value
    harvest target and a large one is a high-value target -- checked
    empirically that data volume is NOT correlated with the OTHER fraud
    typologies in this dataset (mean 57.4 vs 57.8 MB for hndl_risk=True
    vs False), which is expected and correct: quantum exposure is a
    genuinely separate risk dimension from mule/ATO/smurfing fraud, not a
    proxy for it, and this score should not be read as a fraud indicator
  - a trailing component so an account with SUSTAINED weak-crypto usage
    ranks above a single one-off weak session

Usage:
    python3 quantum_engine.py --features data/features/features.csv --output-dir data/quantum
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

TLS_SEVERITY = {"TLSv1.0": 1.0, "TLSv1.2": 0.6, "TLSv1.3": 0.2}
KEY_SIZE_SEVERITY = {1024.0: 1.0, 2048.0: 0.6}  # NaN (PQC/ECDH sessions) -> 0 via fillna below


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--features", type=Path, default=Path("data/features/features.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/quantum"))
    parser.add_argument("--ground-truth-eval", action="store_true")
    args = parser.parse_args()

    cols = [
        "txn_id", "account_id_from", "amount_inr",
        "crypto_tls_version", "crypto_is_pqc_protected", "crypto_is_forward_secret",
        "crypto_key_size_bits", "crypto_data_volume_mb", "crypto_hndl_risk",
        "sender_trailing_weak_crypto_rate", "sender_trailing_hndl_session_count",
    ]
    label_cols = ["label_hndl_exposed", "label_is_fraud"] if args.ground_truth_eval else []
    print(f"Loading {args.features} ...")
    t0 = time.perf_counter()
    df = pd.read_csv(args.features, usecols=cols + label_cols, low_memory=False)
    print(f"  loaded {len(df):,} rows in {time.perf_counter() - t0:.1f}s")

    # --- severity of the crypto weakness itself ---
    df["tls_severity"] = df["crypto_tls_version"].map(TLS_SEVERITY).fillna(0.2)
    df["key_severity"] = df["crypto_key_size_bits"].map(KEY_SIZE_SEVERITY).fillna(0.0)
    df["fs_severity"] = (~df["crypto_is_forward_secret"].fillna(True).astype(bool)).astype(float)
    crypto_severity = (0.4 * df["tls_severity"] + 0.35 * df["key_severity"] + 0.25 * df["fs_severity"]).clip(0, 1)

    # --- value-at-risk: log-scaled data volume, rank-normalized so it's
    # comparable across runs regardless of the lognormal distribution's scale ---
    volume_rank = np.log1p(df["crypto_data_volume_mb"].fillna(0)).rank(pct=True)

    # --- this transaction's own exposure, gated by the deterministic
    # hndl_risk flag (a PQC or forward-secret session is not exposed no
    # matter how large the transfer) ---
    # NOTE: sustained_bonus must be computed INSIDE this gate. Adding it
    # unconditionally to every row was tried first and was a real bug: many
    # accounts have SOME weak-crypto sessions in their trailing history even
    # when their current session is clean (legacy/very_legacy crypto profiles
    # are 20% of the population), so an unconditional add gave a nonzero
    # score to the majority of otherwise-unexposed transactions (65.5% of
    # all rows, vs the true ~10.2% hndl_risk rate) and broke the gate
    # entirely.
    is_exposed = df["crypto_hndl_risk"].fillna(False)
    sustained_bonus = (df["sender_trailing_weak_crypto_rate"].fillna(0) * 0.2).clip(upper=0.2)
    df["quantum_exposure_score"] = np.where(
        is_exposed,
        (0.7 * crypto_severity + 0.3 * volume_rank + sustained_bonus).clip(0, 1),
        0.0,
    )

    def _reason(row: pd.Series) -> str:
        if not row["quantum_exposure_score"] > 0:
            return "modern crypto (PQC or forward-secret) -- not exposed"
        parts = [f"{row['crypto_tls_version']}"]
        if pd.notna(row["crypto_key_size_bits"]):
            parts.append(f"{int(row['crypto_key_size_bits'])}-bit key")
        if not row["crypto_is_forward_secret"]:
            parts.append("no forward secrecy")
        parts.append(f"{row['crypto_data_volume_mb']:.1f}MB transferred")
        if row["sender_trailing_weak_crypto_rate"] and row["sender_trailing_weak_crypto_rate"] > 0.3:
            parts.append(f"sustained pattern ({row['sender_trailing_weak_crypto_rate']:.0%} of recent sessions weak)")
        return ", ".join(parts)

    df["quantum_reason"] = df.apply(_reason, axis=1)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_cols = ["txn_id", "account_id_from", "quantum_exposure_score", "quantum_reason",
                "crypto_tls_version", "crypto_key_size_bits", "crypto_is_forward_secret", "crypto_hndl_risk"]
    out_path = args.output_dir / "quantum_features.csv"
    df[out_cols].to_csv(out_path, index=False)
    print(f"\nWrote {len(df):,} rows to {out_path}")
    print(f"Exposed transactions (score > 0): {(df['quantum_exposure_score'] > 0).sum():,} "
          f"({(df['quantum_exposure_score'] > 0).mean():.2%})")
    print(f"High exposure (score >= 0.7): {(df['quantum_exposure_score'] >= 0.7).sum():,}")

    if args.ground_truth_eval:
        print("\n--- Evaluation (labels used only for reporting) ---")
        match = ((df["quantum_exposure_score"] > 0) == df["label_hndl_exposed"]).mean()
        print(f"quantum_exposure_score>0 matches label_hndl_exposed: {match:.2%} (expected ~100%, same underlying flag)")
        # confirm this is NOT a proxy for mule-type fraud, as designed
        exposed = df[df["quantum_exposure_score"] > 0]
        print(f"label_is_fraud rate among quantum-exposed txns: {exposed['label_is_fraud'].mean():.3%} "
              f"vs baseline {df['label_is_fraud'].mean():.3%} -- should be close, NOT a fraud proxy by design")
        by_severity = df[df["quantum_exposure_score"] > 0].groupby(
            pd.cut(df.loc[df["quantum_exposure_score"] > 0, "quantum_exposure_score"], [0, 0.3, 0.6, 1.0])
        )["crypto_tls_version"].value_counts(normalize=True)
        print("\nTLS version mix by exposure tier:")
        print(by_severity)


if __name__ == "__main__":
    main()
