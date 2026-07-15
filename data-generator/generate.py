from __future__ import annotations

import argparse
import hashlib
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


PERSONAS = ["retail", "hnw", "micro_merchant", "corporate"]
TYPOLOGIES = ["sleeper_mule", "smurfing", "device_farm", "ato", "hndl"]

FIRST_NAMES = [
    "aarav", "vivaan", "aditya", "arjun", "reyaan", "isha", "ananya", "diya",
    "kavya", "riya", "rohan", "kabir", "saanvi", "meera", "nisha", "vedant",
    "priya", "rahul", "neha", "amit", "vikram", "suresh", "deepa", "karan",
]
LAST_NAMES = [
    "sharma", "verma", "iyer", "nair", "reddy", "patel", "gupta", "rao",
    "mehta", "joshi", "khan", "singh", "das", "chopra", "kulkarni", "bose",
]
CITIES = [
    ("Mumbai", 19.0760, 72.8777, 0.14),
    ("Delhi", 28.6139, 77.2090, 0.12),
    ("Bengaluru", 12.9716, 77.5946, 0.11),
    ("Hyderabad", 17.3850, 78.4867, 0.09),
    ("Chennai", 13.0827, 80.2707, 0.08),
    ("Kolkata", 22.5726, 88.3639, 0.07),
    ("Pune", 18.5204, 73.8567, 0.07),
    ("Ahmedabad", 23.0225, 72.5714, 0.06),
    ("Jaipur", 26.9124, 75.7873, 0.05),
    ("Lucknow", 26.8467, 80.9462, 0.05),
    ("Kochi", 9.9312, 76.2673, 0.04),
    ("Indore", 22.7196, 75.8577, 0.04),
    ("Chandigarh", 30.7333, 76.7794, 0.04),
    ("Bhubaneswar", 20.2961, 85.8245, 0.02),
    ("Guwahati", 26.1445, 91.7362, 0.02),
]


def sub_seed(master_seed: int, module_name: str) -> int:
    digest = hashlib.sha256(f"{master_seed}:{module_name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "little")


def rng_for(config: dict[str, Any], module_name: str) -> np.random.Generator:
    return np.random.default_rng(sub_seed(int(config["global"]["master_seed"]), module_name))


def read_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def parse_scalar(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def apply_overrides(config: dict[str, Any], overrides: list[str]) -> None:
    for override in overrides:
        key, value = override.split("=", 1)
        cursor = config
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor[part]
        cursor[parts[-1]] = parse_scalar(value)


def make_one_id(prefix: str, value: int) -> str:
    namespace = int.from_bytes(hashlib.sha256(prefix.encode("utf-8")).digest()[:4], "big")
    return (
        f"{namespace:08x}-"
        f"{(value >> 48) & 0xffff:04x}-"
        f"{0x4000 | ((value >> 32) & 0x0fff):04x}-"
        f"{0x8000 | ((value >> 16) & 0x3fff):04x}-"
        f"{value & 0xffffffffffff:012x}"
    )


def make_ids(prefix: str, n: int, start: int = 0) -> np.ndarray:
    return np.array([make_one_id(prefix, i) for i in range(start, start + n)], dtype=object)


def ensure_dirs(config: dict[str, Any]) -> tuple[Path, Path]:
    out_dir = Path(config["output"]["dir"])
    raw_dir = Path(config["output"]["raw_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    return out_dir, raw_dir


def weighted_choice(rng: np.random.Generator, labels: list[str], weights: dict[str, float], size: int) -> np.ndarray:
    probs = np.array([weights[label] for label in labels], dtype=float)
    probs = probs / probs.sum()
    return rng.choice(labels, size=size, p=probs)


def sample_timestamps(
    rng: np.random.Generator,
    n: int,
    start: pd.Timestamp,
    end: pd.Timestamp,
    hour_weights: list[float],
) -> pd.Series:
    days = pd.date_range(start.normalize(), end.normalize() - pd.Timedelta(days=1), freq="D")
    day_weights = np.array([1.0 if d.weekday() < 5 else 0.7 if d.weekday() == 5 else 0.5 for d in days], dtype=float)
    day_weights = day_weights / day_weights.sum()
    chosen_days = rng.choice(days.values, size=n, p=day_weights)
    hours = rng.choice(np.arange(24), size=n, p=np.array(hour_weights, dtype=float) / np.sum(hour_weights))
    minutes = rng.integers(0, 60, size=n)
    seconds = rng.integers(0, 60, size=n)
    stamps = pd.to_datetime(chosen_days) + pd.to_timedelta(hours, unit="h") + pd.to_timedelta(minutes, unit="m") + pd.to_timedelta(seconds, unit="s")
    return pd.Series(stamps).clip(upper=end - pd.Timedelta(seconds=1))


def haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    radius = 6371.0
    lat1r, lon1r, lat2r, lon2r = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2r - lat1r
    dlon = lon2r - lon1r
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1r) * np.cos(lat2r) * np.sin(dlon / 2) ** 2
    return 2 * radius * np.arcsin(np.sqrt(a))


def build_accounts(config: dict[str, Any]) -> pd.DataFrame:
    rng = rng_for(config, "accounts")
    n = int(config["accounts"]["total"])
    personas = weighted_choice(rng, PERSONAS, config["accounts"]["persona_weights"], n)

    city_names = [city[0] for city in CITIES]
    city_weights = np.array([city[3] for city in CITIES], dtype=float)
    city_weights = city_weights / city_weights.sum()
    city_idx = rng.choice(np.arange(len(CITIES)), size=n, p=city_weights)
    base_lat = np.array([CITIES[i][1] for i in city_idx])
    base_lon = np.array([CITIES[i][2] for i in city_idx])

    balances = np.zeros(n, dtype=float)
    for persona in PERSONAS:
        mask = personas == persona
        mu, sigma = config["accounts"]["balance_lognormal"][persona]
        balances[mask] = rng.lognormal(mu, sigma, mask.sum())

    banks = weighted_choice(rng, list(config["accounts"]["upi_bank_weights"].keys()), config["accounts"]["upi_bank_weights"], n)
    first = rng.choice(FIRST_NAMES, size=n)
    last = rng.choice(LAST_NAMES, size=n)
    suffix = rng.integers(1000, 9999, size=n)

    mobile_ip = rng.random(n) < 0.75
    octet2 = rng.integers(64, 128, size=n)
    ip_a = rng.integers(0, 255, size=n)
    ip_b = rng.integers(1, 255, size=n)
    primary_ip = np.array(
        [
            f"100.{octet2[i]}.{ip_a[i]}.{ip_b[i]}" if mobile_ip[i] else f"10.{ip_a[i]}.{octet2[i]}.{ip_b[i]}"
            for i in range(n)
        ],
        dtype=object,
    )

    start = pd.Timestamp(config["global"]["sim_start"])
    created_offsets = rng.integers(30, 730, size=n)
    accounts = pd.DataFrame(
        {
            "account_id": make_ids("acct", n),
            "upi_id": [f"{first[i]}.{last[i]}{suffix[i]}@{banks[i]}" for i in range(n)],
            "persona": personas,
            "home_lat": np.round(base_lat + rng.normal(0, 0.30, n), 6),
            "home_lon": np.round(base_lon + rng.normal(0, 0.30, n), 6),
            "home_city": np.array(city_names, dtype=object)[city_idx],
            "primary_device_id": make_ids("dev", n),
            "primary_ip": primary_ip.astype(object),
            "initial_balance_inr": np.round(balances, 2),
            "created_at": start - pd.to_timedelta(created_offsets, unit="D"),
        }
    )
    return accounts


def build_ground_truth(config: dict[str, Any], accounts: pd.DataFrame) -> pd.DataFrame:
    rng = rng_for(config, "fraud_assignment")
    n = len(accounts)
    total_fraud = int(config["fraud"]["total_fraud_accounts"])
    fraud_idx = rng.choice(np.arange(n), size=total_fraud, replace=False)
    fraud_set = set(fraud_idx.tolist())

    primary_weights = np.array([0.18, 0.22, 0.16, 0.30, 0.14], dtype=float)
    primary = rng.choice(TYPOLOGIES, size=total_fraud, p=primary_weights / primary_weights.sum())
    type_sets: dict[int, set[str]] = {int(idx): {str(kind)} for idx, kind in zip(fraud_idx, primary)}

    compound_count = int(round(total_fraud * float(config["fraud"]["compound_fraud_fraction"])))
    compound_members = rng.choice(fraud_idx, size=compound_count, replace=False)
    matrix = config["fraud"]["co_occurrence_matrix"]
    for idx in compound_members:
        current = next(iter(type_sets[int(idx)]))
        choices = [kind for kind in TYPOLOGIES if kind != current]
        weights = np.array([matrix[current][kind] for kind in choices], dtype=float)
        if weights.sum() == 0:
            weights = np.ones(len(choices), dtype=float)
        type_sets[int(idx)].add(str(rng.choice(choices, p=weights / weights.sum())))

    # NOTE: is_hard_negative is intentionally NOT assigned here. It used to be a
    # random draw from the clean pool with no corresponding behavior, which meant
    # "hard negative" accounts looked identical to any other clean account. It is
    # now set later, in append_fraud_patterns, by injectors (family_tablet,
    # festive_merchant) that actually give these accounts the unusual-but-legitimate
    # behavior the label is supposed to represent.
    start = pd.Timestamp(config["global"]["sim_start"])
    compromise = [pd.NaT] * n
    cluster_id = [None] * n
    fraud_types = [""] * n
    is_hndl = np.zeros(n, dtype=bool)
    for idx, kinds in type_sets.items():
        day = int(rng.integers(5, 26))
        seconds = int(rng.integers(0, 24 * 3600))
        compromise[idx] = start + pd.Timedelta(days=day - 1, seconds=seconds)
        fraud_types[idx] = "|".join(sorted(kinds))
        is_hndl[idx] = "hndl" in kinds

    for kind in TYPOLOGIES:
        members = [idx for idx, kinds in type_sets.items() if kind in kinds]
        rng.shuffle(members)
        for seq, idx in enumerate(members):
            if kind == "sleeper_mule":
                cluster_id[idx] = f"sleeper_ring_{seq // 11:03d}"
            elif kind == "smurfing":
                cluster_id[idx] = f"smurf_net_{seq // 20:03d}"
            elif kind == "device_farm":
                cluster_id[idx] = f"device_farm_{seq // 8:03d}"
            elif cluster_id[idx] is None:
                cluster_id[idx] = f"{kind}_{seq:04d}"

    return pd.DataFrame(
        {
            "account_id": accounts["account_id"],
            "is_fraud": [i in fraud_set for i in range(n)],
            "fraud_types": fraud_types,
            "is_hndl_risk": is_hndl,
            "is_hard_negative": np.zeros(n, dtype=bool),
            "compromise_timestamp": compromise,
            "cluster_id": cluster_id,
        }
    )


def build_auth_events(config: dict[str, Any], accounts: pd.DataFrame, ground_truth: pd.DataFrame) -> pd.DataFrame:
    rng = rng_for(config, "auth")
    n_auth = int(config["auth"]["target_events"])
    start = pd.Timestamp(config["global"]["sim_start"])
    end = pd.Timestamp(config["global"]["sim_end"])

    rates = np.zeros(len(accounts), dtype=float)
    for persona in PERSONAS:
        mask = accounts["persona"].to_numpy() == persona
        mu, sigma = config["auth"]["nhpp"]["base_rate_lognormal"][persona]
        rates[mask] = rng.lognormal(mu, sigma, mask.sum())
    rates[ground_truth["is_fraud"].to_numpy()] *= 0.82
    rates = rates / rates.sum()

    account_idx = rng.choice(np.arange(len(accounts)), size=n_auth, p=rates)
    timestamps = sample_timestamps(rng, n_auth, start, end, config["auth"]["nhpp"]["time_of_day_weights"])

    burst_fraction = float(config["auth"]["hawkes"].get("burst_fraction", 0.0))
    burst_n = int(n_auth * burst_fraction)
    if burst_n:
        burst_rows = rng.choice(np.arange(n_auth), size=burst_n, replace=False)
        lo, hi = config["auth"]["hawkes"]["burst_delay_minutes"]
        burst_seconds = rng.integers(int(lo * 60), int(hi * 60) + 1, size=burst_n)
        timestamps.iloc[burst_rows] = (
            timestamps.iloc[burst_rows] + pd.to_timedelta(burst_seconds, unit="s")
        ).clip(upper=end - pd.Timedelta(seconds=1))

    home_lat = accounts["home_lat"].to_numpy()[account_idx]
    home_lon = accounts["home_lon"].to_numpy()[account_idx]

    # --- benign noise so these signals are not 100% fraud-exclusive ---
    # (see config auth.legit_noise for rationale). Applied to baseline events
    # only; fraud injectors add their own new-device/failed/travel events on
    # top in append_fraud_patterns.
    noise_cfg = config["auth"].get("legit_noise", {})
    failed_frac = float(noise_cfg.get("failed_login_fraction", 0.0))
    new_dev_frac = float(noise_cfg.get("new_device_fraction", 0.0))
    travel_frac = float(noise_cfg.get("travel_flag_fraction", 0.0))

    is_new_device = rng.random(n_auth) < new_dev_frac
    success = rng.random(n_auth) >= failed_frac  # a small fraction of benign attempts fail

    lat = np.round(home_lat + rng.normal(0, 0.01, n_auth), 6)
    lon = np.round(home_lon + rng.normal(0, 0.01, n_auth), 6)
    # a tiny fraction of benign events are logged from far away (VPN / travel /
    # GeoIP error), which will produce a high velocity vs the previous event
    travel_noise = rng.random(n_auth) < travel_frac
    lat[travel_noise] = np.round(rng.uniform(8.0, 34.0, travel_noise.sum()), 6)   # anywhere in India
    lon[travel_noise] = np.round(rng.uniform(68.0, 92.0, travel_noise.sum()), 6)

    # benign new-device events get a distinct (non-primary) device id, so the
    # device actually differs rather than just flipping the flag
    device_ids = accounts["primary_device_id"].to_numpy()[account_idx].astype(object)
    if is_new_device.any():
        new_dev_ids = np.array([f"newdev-{i:08x}" for i in np.where(is_new_device)[0]], dtype=object)
        device_ids[is_new_device] = new_dev_ids

    auth = pd.DataFrame(
        {
            "auth_event_id": make_ids("auth", n_auth),
            "account_id": accounts["account_id"].to_numpy()[account_idx],
            "timestamp": timestamps.to_numpy(),
            "ip_address": accounts["primary_ip"].to_numpy()[account_idx],
            "device_id": device_ids,
            "is_new_device": is_new_device,
            "success": success,
            "lat": lat,
            "lon": lon,
        }
    )
    auth.sort_values(["account_id", "timestamp"], inplace=True, kind="mergesort")
    prev_lat = auth.groupby("account_id")["lat"].shift(1)
    prev_lon = auth.groupby("account_id")["lon"].shift(1)
    prev_ts = auth.groupby("account_id")["timestamp"].shift(1)
    hours = (auth["timestamp"] - prev_ts).dt.total_seconds() / 3600
    distance = haversine_km(prev_lat.to_numpy(float), prev_lon.to_numpy(float), auth["lat"].to_numpy(float), auth["lon"].to_numpy(float))
    hour_values = hours.to_numpy(float)
    velocity = np.divide(distance, hour_values, out=np.full_like(distance, np.nan), where=hour_values > 0)
    velocity[~np.isfinite(velocity)] = np.nan
    auth["velocity_kmph"] = np.round(velocity, 2)
    auth["impossible_travel"] = auth["velocity_kmph"].fillna(0) > float(config["validation"]["impossible_travel_speed_kmph"])
    auth.sort_values("timestamp", inplace=True, kind="mergesort")
    auth.reset_index(drop=True, inplace=True)
    return auth


def sample_amounts(config: dict[str, Any], personas: np.ndarray, rng: np.random.Generator, drift_days: np.ndarray | None = None) -> np.ndarray:
    n = len(personas)
    amounts = np.zeros(n, dtype=float)
    for persona in PERSONAS:
        mask = personas == persona
        if not mask.any():
            continue
        mu, sigma = config["transactions"]["amount_lognormal"][persona]
        lo, hi = config["transactions"]["amount_bounds_inr"][persona]
        vals = rng.lognormal(mu, sigma, mask.sum())
        amounts[mask] = np.clip(vals, lo, hi)

    if drift_days is not None:
        slopes = rng.normal(*config["drift"]["amount_drift_slope_normal"], n)
        slopes = np.clip(slopes, *config["drift"]["amount_drift_clip"])
        amounts *= 1.0 + slopes * np.clip(drift_days - 1, 0, 29) / 29

    round_mask = rng.random(n) < float(config["transactions"]["round_number_gravity_prob"])
    buckets = np.array(config["transactions"]["round_number_buckets"], dtype=float)
    if round_mask.any():
        vals = amounts[round_mask]
        nearest = buckets[np.argmin(np.abs(vals[:, None] - buckets[None, :]), axis=1)]
        amounts[round_mask] = nearest
    return np.round(amounts, 2)


def choose_receivers(
    config: dict[str, Any],
    accounts: pd.DataFrame,
    senders: np.ndarray,
    fraud_mask: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    n = len(senders)
    receiver_idx = np.empty(n, dtype=int)
    clean_by_persona = {
        persona: np.where((accounts["persona"].to_numpy() == persona) & ~fraud_mask)[0]
        for persona in PERSONAS
    }
    all_clean = np.where(~fraud_mask)[0]
    sender_personas = accounts["persona"].to_numpy()[senders]
    cross = rng.random(n) < float(config["transactions"]["cross_persona_edge_prob"])
    for persona in PERSONAS:
        rows = np.where(sender_personas == persona)[0]
        if len(rows) == 0:
            continue
        same_rows = rows[~cross[rows]]
        cross_rows = rows[cross[rows]]
        pool = clean_by_persona[persona]
        receiver_idx[same_rows] = rng.choice(pool, size=len(same_rows), replace=True)
        if len(cross_rows):
            if persona in {"retail", "hnw"}:
                cross_pool = np.concatenate([clean_by_persona["micro_merchant"], clean_by_persona["retail"], clean_by_persona["hnw"]])
            elif persona == "micro_merchant":
                cross_pool = np.concatenate([clean_by_persona["retail"], clean_by_persona["hnw"]])
            else:
                cross_pool = np.concatenate([clean_by_persona["corporate"], clean_by_persona["hnw"]])
            receiver_idx[cross_rows] = rng.choice(cross_pool if len(cross_pool) else all_clean, size=len(cross_rows), replace=True)
    same = receiver_idx == senders
    if same.any():
        receiver_idx[same] = rng.choice(all_clean, size=same.sum(), replace=True)
    return receiver_idx


def build_transactions(config: dict[str, Any], accounts: pd.DataFrame, ground_truth: pd.DataFrame, auth: pd.DataFrame) -> pd.DataFrame:
    rng = rng_for(config, "transactions")
    n_txn = int(config["transactions"]["target_count"])
    auth_idx = rng.choice(np.arange(len(auth)), size=n_txn, replace=True)

    account_to_idx = pd.Series(np.arange(len(accounts)), index=accounts["account_id"]).to_dict()
    sender_idx = pd.Series(auth["account_id"].to_numpy()[auth_idx]).map(account_to_idx).to_numpy(int)
    fraud_mask = ground_truth["is_fraud"].to_numpy(bool)
    receiver_idx = choose_receivers(config, accounts, sender_idx, fraud_mask, rng)

    auth_ts = pd.to_datetime(auth["timestamp"].to_numpy()[auth_idx])
    session_delay = rng.exponential(float(config["transactions"]["session_duration_mean_minutes"]), n_txn)
    timestamps = pd.Series(auth_ts + pd.to_timedelta(session_delay, unit="m")).clip(upper=pd.Timestamp(config["global"]["sim_end"]) - pd.Timedelta(seconds=1))
    sim_start = pd.Timestamp(config["global"]["sim_start"])
    drift_days = ((timestamps - sim_start).dt.total_seconds() / 86400).to_numpy() + 1
    sender_personas = accounts["persona"].to_numpy()[sender_idx]
    amounts = sample_amounts(config, sender_personas, rng, drift_days=drift_days)

    sender_balance = accounts["initial_balance_inr"].to_numpy()[sender_idx]
    soft_cap = np.maximum(sender_balance * 0.35, 100.0)
    amounts = np.minimum(amounts, soft_cap)
    amounts = np.round(np.maximum(amounts, 1.0), 2)

    receiver_personas = accounts["persona"].to_numpy()[receiver_idx]
    txn_type = np.where(
        sender_personas == "corporate",
        "bulk_settlement",
        np.where(receiver_personas == "micro_merchant", "p2m", "p2p"),
    )

    tx = pd.DataFrame(
        {
            "txn_id": make_ids("txn", n_txn),
            "upi_id_from": accounts["upi_id"].to_numpy()[sender_idx],
            "upi_id_to": accounts["upi_id"].to_numpy()[receiver_idx],
            "amount_inr": amounts,
            "timestamp": timestamps.to_numpy(),
            "txn_type": txn_type,
            "auth_event_id": auth["auth_event_id"].to_numpy()[auth_idx],
            "sender_account_id": accounts["account_id"].to_numpy()[sender_idx],
            "receiver_account_id": accounts["account_id"].to_numpy()[receiver_idx],
            "is_duplicate": False,
        }
    )
    tx.sort_values(["sender_account_id", "timestamp"], inplace=True, kind="mergesort")
    outgoing = tx.groupby("sender_account_id")["amount_inr"].cumsum()
    initial_by_sender = tx["sender_account_id"].map(accounts.set_index("account_id")["initial_balance_inr"])
    tx["balance_after_inr"] = np.round(np.maximum(initial_by_sender.to_numpy(float) - outgoing.to_numpy(float) * 0.35, 0.01), 2)
    tx.sort_values("timestamp", inplace=True, kind="mergesort")
    tx.reset_index(drop=True, inplace=True)
    return tx


def account_indices_for_type(ground_truth: pd.DataFrame, kind: str, accounts: pd.DataFrame | None = None, personas: list[str] | None = None) -> np.ndarray:
    mask = ground_truth["fraud_types"].fillna("").str.contains(kind, regex=False)
    if accounts is not None and personas is not None:
        mask &= accounts["persona"].isin(personas)
    return np.where(mask.to_numpy())[0]


def promote_to_fraud(ground_truth: pd.DataFrame, idx: int, kind: str, cluster_id: str) -> None:
    """Label a previously-clean account as fraud at injection time. Used when
    an injector recruits participants that build_ground_truth did not
    pre-select (e.g. smurfing senders), so the label always matches actual
    injected behavior instead of being guessed in advance."""
    existing = ground_truth.at[idx, "fraud_types"] or ""
    kinds = {k for k in existing.split("|") if k}
    kinds.add(kind)
    ground_truth.at[idx, "fraud_types"] = "|".join(sorted(kinds))
    ground_truth.at[idx, "is_fraud"] = True
    if kind == "hndl":
        ground_truth.at[idx, "is_hndl_risk"] = True
    existing_cid = ground_truth.at[idx, "cluster_id"]
    if existing_cid is None or (isinstance(existing_cid, float) and pd.isna(existing_cid)) or existing_cid == "":
        ground_truth.at[idx, "cluster_id"] = cluster_id


def mark_hard_negative(ground_truth: pd.DataFrame, idx: int, cluster_id: str) -> None:
    """Label an account as a hard negative: behaviorally unusual but not
    fraud. Never sets is_fraud."""
    ground_truth.at[idx, "is_hard_negative"] = True
    existing = ground_truth.at[idx, "cluster_id"]
    # existing may be None OR NaN (pandas coerces an all-None object column to
    # float NaN); `not NaN` is False, which previously skipped this assignment
    # silently and left every hard negative's cluster_id blank.
    if existing is None or (isinstance(existing, float) and pd.isna(existing)) or existing == "":
        ground_truth.at[idx, "cluster_id"] = cluster_id


def append_fraud_patterns(
    config: dict[str, Any],
    accounts: pd.DataFrame,
    ground_truth: pd.DataFrame,
    auth: pd.DataFrame,
    tx: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    rng = rng_for(config, "fraud_injection")
    start = pd.Timestamp(config["global"]["sim_start"])
    end = pd.Timestamp(config["global"]["sim_end"])
    auth_rows: list[dict[str, Any]] = []
    txn_rows: list[dict[str, Any]] = []
    next_auth = len(auth)
    next_txn = len(tx)
    counts = {
        "sleeper_txns": 0,
        "smurf_txns": 0,
        "device_farm_txns": 0,
        "ato_txns": 0,
        "failed_ato_auths": 0,
        "family_tablet_txns": 0,
        "festive_merchant_txns": 0,
    }

    acct_ids = accounts["account_id"].to_numpy()
    upis = accounts["upi_id"].to_numpy()
    primary_ips = accounts["primary_ip"].to_numpy()
    primary_devices = accounts["primary_device_id"].to_numpy()
    lat = accounts["home_lat"].to_numpy(float)
    lon = accounts["home_lon"].to_numpy(float)
    balance = accounts["initial_balance_inr"].to_numpy(float)
    clean_idx = np.where(~ground_truth["is_fraud"].to_numpy(bool))[0]

    def add_auth(acct_idx: int, ts: pd.Timestamp, ip: str, device: str, new_device: bool, success: bool, alat: float, alon: float, velocity: float = 0.0, pattern_type: str = "", scenario_id: str = "") -> str:
        nonlocal next_auth
        auth_id = make_one_id("auth", next_auth)
        next_auth += 1
        auth_rows.append(
            {
                "auth_event_id": auth_id,
                "account_id": acct_ids[acct_idx],
                "timestamp": ts,
                "ip_address": ip,
                "device_id": device,
                "is_new_device": new_device,
                "success": success,
                "lat": round(float(alat), 6),
                "lon": round(float(alon), 6),
                "velocity_kmph": round(float(velocity), 2) if np.isfinite(velocity) else np.nan,
                "impossible_travel": bool(np.isfinite(velocity) and velocity > float(config["validation"]["impossible_travel_speed_kmph"])),
                "pattern_type": pattern_type,
                "scenario_id": scenario_id,
            }
        )
        return auth_id

    def add_txn(sender: int, receiver: int, amount: float, ts: pd.Timestamp, auth_id: str, kind: str, pattern_type: str = "", scenario_id: str = "") -> None:
        nonlocal next_txn
        txn_rows.append(
            {
                "txn_id": make_one_id("txn", next_txn),
                "upi_id_from": upis[sender],
                "upi_id_to": upis[receiver],
                "amount_inr": round(float(amount), 2),
                "timestamp": min(ts, end - pd.Timedelta(seconds=1)),
                "txn_type": "p2m" if accounts["persona"].iloc[receiver] == "micro_merchant" else "p2p",
                "auth_event_id": auth_id,
                "sender_account_id": acct_ids[sender],
                "receiver_account_id": acct_ids[receiver],
                "is_duplicate": False,
                "balance_after_inr": round(max(float(balance[sender]) * 0.40 - float(amount), 0.01), 2),
                "pattern_type": pattern_type,
                "scenario_id": scenario_id,
            }
        )
        next_txn += 1
        counts[kind] += 1

    sleeper = account_indices_for_type(ground_truth, "sleeper_mule")
    if len(sleeper):
        rng.shuffle(sleeper)
        ring_no = 0
        pos = 0
        while pos < len(sleeper) and ring_no < int(config["fraud"]["sleeper_mule"]["n_rings"]):
            lo, hi = config["fraud"]["sleeper_mule"]["accounts_per_ring"]
            size = min(int(rng.integers(lo, hi + 1)), len(sleeper) - pos)
            ring = sleeper[pos : pos + size]
            pos += size
            ring_no += 1
            if len(ring) < 2:
                continue
            c_lo, c_hi = config["fraud"]["sleeper_mule"]["compromise_day_range"]
            t0 = start + pd.Timedelta(days=int(rng.integers(c_lo, c_hi + 1)) - 1, seconds=int(rng.integers(0, 18 * 3600)))
            n_burst = int(rng.integers(*config["fraud"]["sleeper_mule"]["burst_txn_count"]))
            vpn_ip = f"100.{int(rng.integers(64, 128))}.{int(rng.integers(0, 255))}.{int(rng.integers(1, 255))}"
            scenario_id = f"sleeper_ring_{ring_no - 1:03d}"
            for j in range(n_burst):
                sender, receiver = rng.choice(ring, size=2, replace=False)
                ts = t0 + pd.Timedelta(seconds=int(rng.integers(0, int(config["fraud"]["sleeper_mule"]["burst_duration_hours"]) * 3600)))
                auth_id = add_auth(int(sender), ts - pd.Timedelta(seconds=15), vpn_ip, primary_devices[sender], False, True, lat[sender], lon[sender], pattern_type="sleeper_mule", scenario_id=scenario_id)
                amount = rng.lognormal(*config["fraud"]["sleeper_mule"]["burst_amount_lognormal"])
                add_txn(int(sender), int(receiver), amount, ts, auth_id, "sleeper_txns", pattern_type="sleeper_mule", scenario_id=scenario_id)

    smurf = account_indices_for_type(ground_truth, "smurfing")
    retail_clean = np.where((accounts["persona"].to_numpy() == "retail") & ~ground_truth["is_fraud"].to_numpy(bool))[0]
    used_smurf_senders: set[int] = set()
    if len(smurf) and len(retail_clean):
        collectors = smurf[: int(config["fraud"]["smurfing"]["n_networks"])]
        for net_no, collector in enumerate(collectors):
            smurf_count = int(rng.integers(*config["fraud"]["smurfing"]["n_smurfs_per_network"]))
            available = np.array([i for i in retail_clean if i not in used_smurf_senders])
            take = min(smurf_count, len(available))
            if take == 0:
                continue
            senders = rng.choice(available, size=take, replace=False)
            used_smurf_senders.update(int(s) for s in senders)
            scenario_id = f"smurf_net_{net_no:03d}"
            for sender in senders:
                # the sender is the actual structuring participant -- it must be
                # labeled fraud here, since build_ground_truth only pre-labeled
                # the collector and had no way to know who the senders would be
                promote_to_fraud(ground_truth, int(sender), "smurfing", scenario_id)
            day = int(rng.integers(8, 28))
            t0 = start + pd.Timedelta(days=day - 1, hours=int(rng.integers(9, 20)))
            window = int(rng.integers(*config["fraud"]["smurfing"]["window_minutes"]))
            for sender in senders:
                ts = t0 + pd.Timedelta(seconds=int(rng.integers(0, window * 60)))
                auth_id = add_auth(int(sender), ts - pd.Timedelta(seconds=20), primary_ips[sender], primary_devices[sender], False, True, lat[sender], lon[sender], pattern_type="smurfing", scenario_id=scenario_id)
                lo, hi = config["fraud"]["smurfing"]["amount_range_inr"]
                add_txn(int(sender), int(collector), float(rng.uniform(lo, hi)), ts, auth_id, "smurf_txns", pattern_type="smurfing", scenario_id=scenario_id)

    farms = account_indices_for_type(ground_truth, "device_farm")
    if len(farms):
        rng.shuffle(farms)
        size = int(config["fraud"]["device_farm"]["accounts_per_cluster"])
        for cluster_no in range(min(int(config["fraud"]["device_farm"]["n_clusters"]), len(farms) // size)):
            cluster = farms[cluster_no * size : (cluster_no + 1) * size]
            t0 = start + pd.Timedelta(days=int(rng.integers(6, 29)) - 1, hours=int(rng.integers(8, 22)))
            shared_device = make_one_id("dev_farm", cluster_no)
            shared_ip = f"100.{int(rng.integers(64, 128))}.{int(rng.integers(0, 255))}.{int(rng.integers(1, 255))}"
            repeats = int(rng.integers(*config["fraud"]["device_farm"]["n_cycle_repeats"]))
            cursor = t0
            mean_gap = float(config["fraud"]["device_farm"]["inter_hop_mean_minutes"])
            if t0.day > 15:
                mean_gap = float(config["drift"]["device_farm_late_interval_minutes"])
            scenario_id = f"device_farm_{cluster_no:03d}"
            for _ in range(repeats):
                for i, sender in enumerate(cluster):
                    receiver = cluster[(i + 1) % len(cluster)]
                    cursor += pd.Timedelta(minutes=float(rng.exponential(mean_gap)))
                    auth_id = add_auth(int(sender), cursor - pd.Timedelta(seconds=10), shared_ip, shared_device, True, True, lat[sender], lon[sender], pattern_type="device_farm", scenario_id=scenario_id)
                    amount = rng.lognormal(*config["fraud"]["device_farm"]["hop_amount_lognormal"])
                    add_txn(int(sender), int(receiver), amount, cursor, auth_id, "device_farm_txns", pattern_type="device_farm", scenario_id=scenario_id)

    # recompute: smurfing promotions above may have moved accounts out of "clean"
    clean_idx = np.where(~ground_truth["is_fraud"].to_numpy(bool))[0]
    ato = account_indices_for_type(ground_truth, "ato", accounts=accounts, personas=["retail"])
    sleeper_targets = sleeper if len(sleeper) else clean_idx
    if len(ato):
        selected = ato[: int(config["fraud"]["ato"]["n_accounts"])]
        attacker_city = np.array([city for city in CITIES if city[0] != "Mumbai"], dtype=object)
        for acct in selected:
            scenario_id = f"ato_{int(acct):06d}"
            city = attacker_city[int(rng.integers(0, len(attacker_city)))]
            t0 = start + pd.Timedelta(days=int(rng.integers(10, 29)) - 1, hours=int(rng.integers(8, 22)))
            bad_ip = f"100.{int(rng.integers(64, 128))}.{int(rng.integers(0, 255))}.{int(rng.integers(1, 255))}"
            bad_dev = make_one_id("ato_dev", int(acct))
            for delay in [0, 30, 90, 210]:
                add_auth(int(acct), t0 + pd.Timedelta(seconds=delay), bad_ip, bad_dev, True, False, float(city[1]), float(city[2]), velocity=1500, pattern_type="ato", scenario_id=scenario_id)
                counts["failed_ato_auths"] += 1
            success_ts = t0 + pd.Timedelta(seconds=260)
            auth_id = add_auth(int(acct), success_ts, bad_ip, bad_dev, True, True, float(city[1]), float(city[2]), velocity=1500, pattern_type="ato", scenario_id=scenario_id)
            cashouts = int(rng.integers(*config["fraud"]["ato"]["cash_out_txn_count"]))
            for _ in range(cashouts):
                target_pool = sleeper_targets if rng.random() < float(config["fraud"]["ato"]["ato_to_ring_linkage_prob"]) else clean_idx
                receiver = int(rng.choice(target_pool))
                ts = success_ts + pd.Timedelta(minutes=float(rng.uniform(1, 5)))
                amount = rng.lognormal(*config["fraud"]["ato"]["cash_out_amount_lognormal"])
                add_txn(int(acct), receiver, amount, ts, auth_id, "ato_txns", pattern_type="ato", scenario_id=scenario_id)

    # --- Hard negative: family_tablet ---
    # A small group sharing one device, like device_farm structurally, but with
    # low-frequency, low-amount transfers spread across the whole window -- the
    # cadence of a family sharing a tablet, not a rented device farm.
    fam_cfg = config["fraud"]["hard_negatives"]["family_tablet"]
    hn_pool = np.where(
        (accounts["persona"].to_numpy() == "retail")
        & ~ground_truth["is_fraud"].to_numpy(bool)
        & ~ground_truth["is_hard_negative"].to_numpy(bool)
    )[0]
    rng.shuffle(hn_pool)
    per_group = int(fam_cfg["accounts_per_group"])
    pos = 0
    for group_no in range(int(fam_cfg["n_groups"])):
        if pos + per_group > len(hn_pool):
            break
        group = hn_pool[pos : pos + per_group]
        pos += per_group
        scenario_id = f"family_tablet_{group_no:03d}"
        shared_device = make_one_id("family_dev", group_no)
        for idx in group:
            mark_hard_negative(ground_truth, int(idx), scenario_id)
        n_transfers = int(rng.integers(4, 10))
        for _ in range(n_transfers):
            sender, receiver = rng.choice(group, size=2, replace=False)
            ts = start + pd.Timedelta(
                days=int(rng.integers(0, 29)), hours=int(rng.integers(8, 22)), minutes=int(rng.integers(0, 60))
            )
            auth_id = add_auth(
                int(sender), ts - pd.Timedelta(seconds=10), primary_ips[sender], shared_device, False, True,
                lat[sender], lon[sender], pattern_type="family_tablet_hard_negative", scenario_id=scenario_id,
            )
            amount = float(rng.uniform(100, 3000))
            add_txn(int(sender), int(receiver), amount, ts, auth_id, "family_tablet_txns", pattern_type="family_tablet_hard_negative", scenario_id=scenario_id)

    # --- Hard negative: festive_merchant ---
    # A legitimate merchant's volume spikes during a festival window because many
    # distinct, unrelated customers are shopping -- not because funds are being
    # layered through a small coordinated group like smurfing.
    fest_cfg = config["fraud"]["hard_negatives"]["festive_merchant"]
    merchant_pool = np.where(
        (accounts["persona"].to_numpy() == "micro_merchant")
        & ~ground_truth["is_fraud"].to_numpy(bool)
        & ~ground_truth["is_hard_negative"].to_numpy(bool)
    )[0]
    rng.shuffle(merchant_pool)
    merchants = merchant_pool[: int(fest_cfg["n_merchants"])]
    fest_lo, fest_hi = fest_cfg["festival_day_range"]
    volume_multiplier = float(fest_cfg["volume_multiplier"])
    customer_pool = np.where((accounts["persona"].to_numpy() == "retail") & ~ground_truth["is_fraud"].to_numpy(bool))[0]
    base_daily_txns = 6
    extra_txns = int(base_daily_txns * (volume_multiplier - 1.0)) * (fest_hi - fest_lo + 1)
    lo_amt, hi_amt = config["transactions"]["amount_bounds_inr"]["retail"]
    for merch_no, merchant in enumerate(merchants):
        scenario_id = f"festive_merchant_{merch_no:03d}"
        mark_hard_negative(ground_truth, int(merchant), scenario_id)
        customers = rng.choice(customer_pool, size=min(extra_txns, len(customer_pool)), replace=True)
        for cust in customers:
            day = int(rng.integers(fest_lo, fest_hi + 1))
            ts = start + pd.Timedelta(days=day - 1, hours=int(rng.integers(9, 21)), minutes=int(rng.integers(0, 60)))
            auth_id = add_auth(
                int(cust), ts - pd.Timedelta(seconds=10), primary_ips[cust], primary_devices[cust], False, True,
                lat[cust], lon[cust], pattern_type="festive_merchant_hard_negative", scenario_id=scenario_id,
            )
            amount = float(rng.uniform(max(lo_amt, 100), min(hi_amt, 5000)))
            add_txn(int(cust), int(merchant), amount, ts, auth_id, "festive_merchant_txns", pattern_type="festive_merchant_hard_negative", scenario_id=scenario_id)

    injected_tx_df = pd.DataFrame(txn_rows) if txn_rows else pd.DataFrame(columns=["sender_account_id", "receiver_account_id", "pattern_type", "scenario_id", "timestamp"])
    injected_auth_df = pd.DataFrame(auth_rows) if auth_rows else pd.DataFrame(columns=["account_id", "pattern_type", "scenario_id", "timestamp"])
    reconcile_counts = reconcile_ground_truth(ground_truth, injected_tx_df, injected_auth_df)
    counts.update(reconcile_counts)

    if auth_rows:
        auth = pd.concat([auth, pd.DataFrame(auth_rows)], ignore_index=True)
    if txn_rows:
        tx = pd.concat([tx, pd.DataFrame(txn_rows)], ignore_index=True)
    tx.sort_values("timestamp", inplace=True, kind="mergesort")
    tx.reset_index(drop=True, inplace=True)
    auth.sort_values("timestamp", inplace=True, kind="mergesort")
    auth.reset_index(drop=True, inplace=True)
    return auth, tx, counts


def reconcile_ground_truth(ground_truth: pd.DataFrame, injected_tx: pd.DataFrame, injected_auth: pd.DataFrame) -> dict[str, int]:
    """Make ground truth match what was actually generated, not what was
    planned. build_ground_truth pre-assigns typology labels and a guessed
    cluster_id before any behavior exists; injection caps (n_rings,
    n_clusters, n_accounts, or the first-N collectors for smurfing) mean not
    every pre-labeled account ends up with real injected behavior. For each
    reconciled typology, this drops the label from accounts with no actual
    participation, and for accounts that keep the label, overwrites
    cluster_id and compromise_timestamp with the real scenario_id and the
    real earliest event timestamp observed in the injected rows.

    hndl is exempt: it modifies existing baseline transactions rather than
    injecting new rows, so every hndl-labeled account is genuinely affected
    by construction.
    """
    reconciled_types = {"sleeper_mule", "smurfing", "device_farm", "ato"}
    participants: dict[str, set] = {k: set() for k in reconciled_types}
    scenario_by_account: dict[tuple[str, Any], str] = {}
    earliest_ts_by_account: dict[tuple[str, Any], pd.Timestamp] = {}

    def _note(kind: str, acct: Any, ts: Any, scenario_id: Any) -> None:
        participants[kind].add(acct)
        scenario_by_account.setdefault((kind, acct), scenario_id)
        ts = pd.Timestamp(ts)
        key = (kind, acct)
        if key not in earliest_ts_by_account or ts < earliest_ts_by_account[key]:
            earliest_ts_by_account[key] = ts

    for kind in reconciled_types:
        if len(injected_tx):
            for _, row in injected_tx.loc[injected_tx["pattern_type"] == kind].iterrows():
                _note(kind, row["sender_account_id"], row["timestamp"], row["scenario_id"])
                _note(kind, row["receiver_account_id"], row["timestamp"], row["scenario_id"])
        if len(injected_auth):
            for _, row in injected_auth.loc[injected_auth["pattern_type"] == kind].iterrows():
                _note(kind, row["account_id"], row["timestamp"], row["scenario_id"])

    dropped_counts = {f"reconciled_dropped_{k}": 0 for k in reconciled_types}
    fraud_rows = ground_truth.index[ground_truth["fraud_types"] != ""]
    for i in fraud_rows:
        acct_id = ground_truth.at[i, "account_id"]
        types = ground_truth.at[i, "fraud_types"]
        kinds = {k for k in types.split("|") if k} if types else set()
        keep = set()
        best_ts = None
        for kind in kinds:
            if kind in reconciled_types:
                if acct_id not in participants[kind]:
                    dropped_counts[f"reconciled_dropped_{kind}"] += 1
                    continue
                ground_truth.at[i, "cluster_id"] = scenario_by_account[(kind, acct_id)]
                ts = earliest_ts_by_account[(kind, acct_id)]
                best_ts = ts if best_ts is None or ts < best_ts else best_ts
            keep.add(kind)
        if keep != kinds:
            ground_truth.at[i, "fraud_types"] = "|".join(sorted(keep))
            if not keep:
                ground_truth.at[i, "is_fraud"] = False
                ground_truth.at[i, "cluster_id"] = None
                ground_truth.at[i, "compromise_timestamp"] = pd.NaT
                continue
        if best_ts is not None:
            ground_truth.at[i, "compromise_timestamp"] = best_ts.as_unit("us")
    return dropped_counts


def apply_degradation(config: dict[str, Any], auth: pd.DataFrame, tx: pd.DataFrame, fast: bool) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    if fast:
        return auth, tx, {"skipped_fast_mode": 1}
    rng = rng_for(config, "degradation")
    stats: dict[str, int] = {}

    n_jitter = int(len(auth) * float(config["degradation"]["timestamp_jitter_fraction"]))
    jitter_rows = rng.choice(auth.index.to_numpy(), size=n_jitter, replace=False)
    jitter = -np.abs(rng.normal(0, float(config["degradation"]["timestamp_jitter_std_seconds"]), n_jitter))
    sim_start = pd.Timestamp(config["global"]["sim_start"])
    sim_end = pd.Timestamp(config["global"]["sim_end"])
    auth.loc[jitter_rows, "timestamp"] = (
        pd.to_datetime(auth.loc[jitter_rows, "timestamp"]) + pd.to_timedelta(jitter, unit="s")
    ).clip(lower=sim_start, upper=sim_end - pd.Timedelta(seconds=1))
    stats["auth_timestamp_jitter_rows"] = n_jitter

    n_null = int(len(auth) * float(config["degradation"]["null_fraction_auth_fields"]))
    for field in ["ip_address", "device_id", "lat", "lon"]:
        rows = rng.choice(auth.index.to_numpy(), size=n_null, replace=False)
        auth.loc[rows, field] = np.nan
    stats["auth_null_rows_per_field"] = n_null

    n_geo = int(len(auth) * float(config["degradation"]["geoip_noise_fraction"]))
    rows = rng.choice(auth.index.to_numpy(), size=n_geo, replace=False)
    city_idx = rng.integers(0, len(CITIES), size=n_geo)
    auth.loc[rows, "lat"] = np.round(np.array([CITIES[i][1] for i in city_idx]) + rng.normal(0, float(config["degradation"]["geoip_noise_std_degrees"]), n_geo), 6)
    auth.loc[rows, "lon"] = np.round(np.array([CITIES[i][2] for i in city_idx]) + rng.normal(0, float(config["degradation"]["geoip_noise_std_degrees"]), n_geo), 6)
    stats["auth_geoip_noise_rows"] = n_geo

    n_dupe = int(len(tx) * float(config["degradation"]["duplicate_txn_fraction"]))
    dupe_rows = rng.choice(tx.index.to_numpy(), size=n_dupe, replace=False)
    dupes = tx.loc[dupe_rows].copy()
    dupes["txn_id"] = make_ids("txn_dup", n_dupe)
    delay_lo, delay_hi = config["degradation"]["duplicate_timestamp_delay_seconds"]
    dupes["timestamp"] = (
        pd.to_datetime(dupes["timestamp"]) + pd.to_timedelta(rng.uniform(delay_lo, delay_hi, n_dupe), unit="s")
    ).clip(upper=sim_end - pd.Timedelta(seconds=1))
    dupes["is_duplicate"] = True
    tx = pd.concat([tx, dupes], ignore_index=True)
    tx.sort_values("timestamp", inplace=True, kind="mergesort")
    tx.reset_index(drop=True, inplace=True)
    stats["duplicate_transaction_rows"] = n_dupe
    return auth, tx, stats


def build_crypto(config: dict[str, Any], accounts: pd.DataFrame, ground_truth: pd.DataFrame, tx: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    rng = rng_for(config, "crypto")
    n = len(tx)
    profiles = list(config["crypto"]["profile_weights"].keys())
    weights = np.array(list(config["crypto"]["profile_weights"].values()), dtype=float)
    chosen = rng.choice(profiles, size=n, p=weights / weights.sum())
    sender_persona = tx["sender_account_id"].map(accounts.set_index("account_id")["persona"]).to_numpy()
    data_volume = np.zeros(n, dtype=float)
    for persona in PERSONAS:
        mask = sender_persona == persona
        mu, sigma = config["crypto"]["data_volume_lognormal"][persona]
        data_volume[mask] = rng.lognormal(mu, sigma, mask.sum())

    tls = np.empty(n, dtype=object)
    cipher = np.empty(n, dtype=object)
    kx = np.empty(n, dtype=object)
    key_size = np.full(n, np.nan)
    pqc = np.zeros(n, dtype=bool)
    fs = np.ones(n, dtype=bool)

    profile_map = {
        "modern_pqc": ("TLSv1.3", "TLS_AES_256_GCM_SHA384", "X25519Kyber768", np.nan, True, True),
        "modern_non_pqc": ("TLSv1.3", "TLS_AES_128_GCM_SHA256", "ECDH", np.nan, False, True),
        "legacy_migrated": ("TLSv1.2", "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384", "ECDHE-RSA", np.nan, False, True),
        "legacy_static_rsa": ("TLSv1.2", "TLS_RSA_WITH_AES_256_CBC_SHA", "RSA", 2048, False, False),
        "very_legacy": ("TLSv1.0", "TLS_RSA_WITH_3DES_EDE_CBC_SHA", "RSA", 1024, False, False),
    }
    for profile, values in profile_map.items():
        mask = chosen == profile
        tls[mask], cipher[mask], kx[mask], key_size[mask], pqc[mask], fs[mask] = values

    hndl_accounts = set(ground_truth.loc[ground_truth["is_hndl_risk"], "account_id"])
    hndl_tx = tx["sender_account_id"].isin(hndl_accounts).to_numpy()
    if hndl_tx.any():
        tls[hndl_tx] = "TLSv1.2"
        cipher[hndl_tx] = "TLS_RSA_WITH_AES_256_CBC_SHA"
        kx[hndl_tx] = "RSA"
        key_size[hndl_tx] = rng.choice(config["fraud"]["hndl"]["key_sizes"], size=hndl_tx.sum())
        pqc[hndl_tx] = False
        fs[hndl_tx] = False

    null_count = int(n * float(config["degradation"]["null_fraction_crypto_fields"]))
    null_rows = rng.choice(np.arange(n), size=null_count, replace=False)
    data_volume[null_rows] = np.nan

    crypto = pd.DataFrame(
        {
            "session_id": make_ids("sess", n),
            "txn_id": tx["txn_id"].to_numpy(),
            "tls_version": tls,
            "cipher_suite": cipher,
            "key_exchange": kx,
            "key_size_bits": key_size,
            "is_pqc_protected": pqc,
            "is_forward_secret": fs,
            "data_volume_mb": np.round(data_volume, 4),
            "hndl_risk": (~fs) & (~pqc),
        }
    )
    return crypto, {"crypto_data_volume_null_rows": null_count}


def make_splits(config: dict[str, Any], tx: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    start = pd.Timestamp(config["global"]["sim_start"])
    day = ((pd.to_datetime(tx["timestamp"]) - start).dt.total_seconds() / 86400) + 1
    cols = ["txn_id", "sender_account_id"]
    split_df = tx[cols].rename(columns={"sender_account_id": "account_id"}).copy()
    train = split_df[(day >= 1) & (day <= 20)].copy()
    val = split_df[(day > 20) & (day <= 25)].copy()
    holdout = split_df[(day > 25)].copy()
    train["split"] = "train"
    val["split"] = "val"
    holdout["split"] = "holdout"
    return train, val, holdout


def validate(config: dict[str, Any], accounts: pd.DataFrame, auth: pd.DataFrame, tx: pd.DataFrame, crypto: pd.DataFrame, ground_truth: pd.DataFrame) -> list[str]:
    results: list[str] = []
    account_ids = set(accounts["account_id"])
    upis = set(accounts["upi_id"])
    auth_ids = set(auth["auth_event_id"])
    txn_ids = set(tx["txn_id"])

    assert set(tx["upi_id_from"]).issubset(upis)
    assert set(tx["upi_id_to"]).issubset(upis)
    assert set(tx["auth_event_id"]).issubset(auth_ids)
    assert set(crypto["txn_id"]).issubset(txn_ids)
    assert set(ground_truth["account_id"]).issubset(account_ids)
    results.append("PASS referential_integrity")

    assert tx["balance_after_inr"].min() >= -1.0
    start = pd.Timestamp(config["global"]["sim_start"])
    end = pd.Timestamp(config["global"]["sim_end"])
    assert pd.to_datetime(tx["timestamp"]).between(start, end).all()
    results.append("PASS temporal_and_balance_bounds")

    fraud_accounts = set(ground_truth.loc[ground_truth["is_fraud"], "account_id"])
    fraud_txn = tx["sender_account_id"].isin(fraud_accounts) | tx["receiver_account_id"].isin(fraud_accounts)
    fraud_txn_prev = float(fraud_txn.mean())
    lo, hi = config["validation"]["fraud_txn_prevalence_range"]
    status = "PASS" if lo <= fraud_txn_prev <= hi else "WARN"
    results.append(f"{status} fraud_txn_prevalence={fraud_txn_prev:.4%}")

    fraud_account_prev = float(ground_truth["is_fraud"].mean())
    lo, hi = config["validation"]["fraud_account_prevalence_range"]
    assert lo <= fraud_account_prev <= hi
    results.append(f"PASS fraud_account_prevalence={fraud_account_prev:.4%}")

    assert bool((crypto.loc[crypto["hndl_risk"], "is_forward_secret"] == False).all())
    assert not bool(((crypto["tls_version"] == "TLSv1.3") & (crypto["key_exchange"] == "X25519Kyber768") & (crypto["hndl_risk"])).any())
    soft_warning = int(((crypto["tls_version"] == "TLSv1.3") & (~crypto["is_pqc_protected"])).sum())
    results.append(f"PASS hndl_correctness soft_non_pqc_tls13_rows={soft_warning}")

    impossible = auth["impossible_travel"].fillna(False).to_numpy(bool)
    assert (auth.loc[impossible, "velocity_kmph"] > float(config["validation"]["impossible_travel_speed_kmph"])).all()
    results.append("PASS velocity_check")
    return results


def write_outputs(
    config: dict[str, Any],
    out_dir: Path,
    accounts: pd.DataFrame,
    auth: pd.DataFrame,
    tx: pd.DataFrame,
    crypto: pd.DataFrame,
    ground_truth: pd.DataFrame,
    splits: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame],
) -> None:
    accounts.to_csv(out_dir / "accounts.csv", index=False)
    tx_out = tx[
        [
            "txn_id",
            "upi_id_from",
            "upi_id_to",
            "amount_inr",
            "timestamp",
            "txn_type",
            "auth_event_id",
            "balance_after_inr",
            "is_duplicate",
            "pattern_type",
            "scenario_id",
        ]
    ].fillna({"pattern_type": "", "scenario_id": ""})
    tx_out.to_csv(out_dir / "transactions.csv", index=False)
    auth.fillna({"pattern_type": "", "scenario_id": ""}).to_csv(out_dir / "auth_events.csv", index=False)
    crypto.to_csv(out_dir / "crypto_sessions.csv", index=False)
    ground_truth.to_csv(out_dir / "ground_truth.csv", index=False)
    names = ["train_ids.csv", "val_ids.csv", "holdout_ids.csv"]
    for name, split in zip(names, splits):
        split.to_csv(out_dir / name, index=False)


def make_summary(
    config: dict[str, Any],
    elapsed: float,
    accounts: pd.DataFrame,
    auth: pd.DataFrame,
    tx: pd.DataFrame,
    crypto: pd.DataFrame,
    ground_truth: pd.DataFrame,
    splits: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame],
    fraud_counts: dict[str, int],
    degradation_stats: dict[str, int],
    crypto_stats: dict[str, int],
    validation: list[str],
) -> str:
    fraud_accounts = set(ground_truth.loc[ground_truth["is_fraud"], "account_id"])
    split_lines = []
    for name, split in zip(["train", "val", "holdout"], splits):
        fraud_pct = split["account_id"].isin(fraud_accounts).mean() if len(split) else 0
        split_lines.append(f"  {name}: {len(split):,} transactions, sender-fraud {fraud_pct:.2%}")

    breakdown = ground_truth.loc[ground_truth["is_fraud"], "fraud_types"].str.get_dummies(sep="|").sum().sort_values(ascending=False)
    lines = [
        "Synthetic Fraud Dataset Run Summary",
        f"Master seed: {config['global']['master_seed']}",
        f"Accounts: {len(accounts):,}",
        "Persona counts:",
        *(f"  {k}: {v:,}" for k, v in accounts["persona"].value_counts().sort_index().items()),
        f"Transactions: {len(tx):,}",
        f"Auth events: {len(auth):,}",
        f"Crypto sessions: {len(crypto):,}",
        f"Fraud accounts: {int(ground_truth['is_fraud'].sum()):,} ({ground_truth['is_fraud'].mean():.2%})",
        "Fraud type breakdown:",
        *(f"  {k}: {int(v):,}" for k, v in breakdown.items()),
        f"Compound fraud accounts: {int(ground_truth.loc[ground_truth['is_fraud'], 'fraud_types'].str.contains('|', regex=False).sum()):,}",
        f"Hard negatives: {int(ground_truth['is_hard_negative'].sum()):,}",
        f"HNDL-risk sessions: {int(crypto['hndl_risk'].sum()):,}",
        "Injected fraud rows:",
        *(f"  {k}: {v:,}" for k, v in fraud_counts.items()),
        "Degradation layer:",
        *(f"  {k}: {v:,}" for k, v in {**degradation_stats, **crypto_stats}.items()),
        "Validation:",
        *(f"  {line}" for line in validation),
        "Temporal split:",
        *split_lines,
        f"Generation time seconds: {elapsed:.2f}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic UPI fraud CSV dataset.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--set", action="append", default=[], help="Override scalar config value, e.g. accounts.total=1000")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fast", action="store_true", help="Skip slower degradation details.")
    args = parser.parse_args()

    config = read_config(Path(args.config))
    apply_overrides(config, args.set)
    out_dir, _ = ensure_dirs(config)
    print(f"Output directory: {out_dir}")
    if args.dry_run:
        print(yaml.safe_dump(config, sort_keys=False))
        return

    t0 = time.perf_counter()
    accounts = build_accounts(config)
    ground_truth = build_ground_truth(config, accounts)
    auth = build_auth_events(config, accounts, ground_truth)
    tx = build_transactions(config, accounts, ground_truth, auth)
    auth, tx, fraud_counts = append_fraud_patterns(config, accounts, ground_truth, auth, tx)
    auth, tx, degradation_stats = apply_degradation(config, auth, tx, args.fast)
    crypto, crypto_stats = build_crypto(config, accounts, ground_truth, tx)
    splits = make_splits(config, tx)
    validation = validate(config, accounts, auth, tx, crypto, ground_truth)
    write_outputs(config, out_dir, accounts, auth, tx, crypto, ground_truth, splits)
    elapsed = time.perf_counter() - t0
    summary = make_summary(
        config,
        elapsed,
        accounts,
        auth,
        tx,
        crypto,
        ground_truth,
        splits,
        fraud_counts,
        degradation_stats,
        crypto_stats,
        validation,
    )
    (out_dir / "run_summary.txt").write_text(summary, encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
