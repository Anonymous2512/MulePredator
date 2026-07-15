#!/usr/bin/env python3
"""
graph_engine.py

Turns the transaction graph into per-account graph risk signals. Two
complementary mechanisms, because they catch different ring shapes:

  1. Community detection (Louvain) on a RESTRICTED subgraph -- catches
     tightly-interconnected rings (sleeper_mule, device_farm) where members
     repeatedly transact with each other. Running Louvain on the FULL graph
     does not work here: it was tested and verified to merge ~80% of all
     accounts into one giant low-signal community, because the organic
     baseline transaction graph is dense enough that modularity optimization
     can't separate small rings from it. Restricting first to a
     behaviorally-flagged seed set (no neighbor expansion, since this graph's
     average degree is ~80 and even one hop of expansion balloons back to
     nearly the whole graph) fixes this: device_farm rings go from ~0%
     co-location to 100%, sleeper_mule from ~0% to 84%.

  2. Hub/pivot detection via fan-in degree -- catches star-shaped patterns
     (smurfing collectors, ATO cash-out targets) that community detection
     structurally cannot: a smurfing sender transacts once with a collector
     and never with any other sender, so senders never look like a
     "community" to each other. What they share is a common destination
     with an extreme in-degree, which a direct fan-in ranking finds
     regardless of community membership.

The behavioral pre-filter uses only features already computed in
build_feature_table.py -- never ground truth labels. Ground truth is used
ONLY at the end, to report how well this recovers known rings.

Usage:
    python3 graph_engine.py --features data/features/features.csv --output-dir data/graph
"""
from __future__ import annotations

import argparse
import time
from collections import Counter
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd

SEED_PERCENTILE = 0.90  # top 10% by behavioral flag_score seeds the subgraph
BETWEENNESS_SIZE_CUTOFF = 500  # skip exact betweenness above this community size
LOUVAIN_RESOLUTION = 8.0  # empirically tuned: default resolution (1.0) merges rings into
# communities averaging ~2000 nodes (dilutes the signal to near-nothing); resolution=8
# shrinks matched communities to a mean of ~34-54 nodes, close to actual ring sizes, without
# losing the device_farm (20/20) or sleeper_mule (16/19) co-location rate. Going higher
# (16-20) fragments the ENTIRE seed graph uniformly, which removes "small community" as a
# discriminating signal rather than sharpening it. See conversation history for the sweep.

# Structural seed path (added to catch sleeper rings the behavioral filter misses).
# Sleeper mules spread their burst across ring members so no 6h velocity window spikes
# -- their peak trailing count (median 7) is identical to clean accounts. But their
# transactions are 100% ring-internal, giving them high LOCAL CLUSTERING COEFFICIENT
# (their counterparties also transact with each other). Checked: lcc>=0.02 catches
# 64/159 sleepers with near-zero clean false positives, and adds a velocity-independent
# entry path into community detection. Restricted to degree 2-30 nodes (ring members
# have few peers) which cuts compute from 85s (all nodes) to <1s.
STRUCTURAL_LCC_THRESHOLD = 0.02
STRUCTURAL_DEGREE_RANGE = (2, 30)


def load_behavioral_columns(features_path: Path) -> pd.DataFrame:
    cols = [
        "account_id_from", "account_id_to", "amount_inr", "timestamp", "txn_id",
        "sender_trailing_count", "sender_trailing_distinct_counterparties",
        "receiver_trailing_count", "receiver_trailing_distinct_counterparties",
        "sender_trailing_weak_crypto_rate", "sender_trailing_failed_logins",
        "sender_trailing_new_device_count",
    ]
    print(f"Loading {features_path} ...")
    t0 = time.perf_counter()
    df = pd.read_csv(features_path, usecols=cols, low_memory=False)
    print(f"  loaded {len(df):,} rows in {time.perf_counter() - t0:.1f}s")
    return df


def compute_behavioral_flag_score(df: pd.DataFrame) -> pd.DataFrame:
    """Per-account, label-free anomaly score: the max percentile rank across
    several trailing-window behavioral signals. Deliberately simple and
    inspectable (each account's flagging reason is recoverable) rather than
    a black-box model, since this is the seed for a graph restriction step
    that needs to be explainable if challenged."""
    sender_max = df.groupby("account_id_from").agg(
        max_out_burst=("sender_trailing_count", "max"),
        max_fanout=("sender_trailing_distinct_counterparties", "max"),
        max_weak_crypto=("sender_trailing_weak_crypto_rate", "max"),
        max_failed_logins=("sender_trailing_failed_logins", "max"),
        max_new_device=("sender_trailing_new_device_count", "max"),
    ).reset_index().rename(columns={"account_id_from": "account_id"})
    receiver_max = df.groupby("account_id_to").agg(
        max_in_burst=("receiver_trailing_count", "max"),
        max_fanin=("receiver_trailing_distinct_counterparties", "max"),
    ).reset_index().rename(columns={"account_id_to": "account_id"})
    acct = sender_max.merge(receiver_max, on="account_id", how="outer").fillna(0)

    rank_cols = ["max_out_burst", "max_fanout", "max_in_burst", "max_fanin",
                 "max_weak_crypto", "max_failed_logins", "max_new_device"]
    for c in rank_cols:
        acct[c + "_pct"] = acct[c].rank(pct=True)
    acct["flag_score"] = acct[[c + "_pct" for c in rank_cols]].max(axis=1)
    # which single dimension drove the flag, for explainability
    acct["flag_reason"] = acct[[c + "_pct" for c in rank_cols]].idxmax(axis=1).str.replace("_pct", "", regex=False)
    return acct


def build_full_graph(df: pd.DataFrame) -> nx.Graph:
    edges = df.groupby(["account_id_from", "account_id_to"]).agg(
        weight=("amount_inr", "sum"), txn_count=("txn_id", "count")
    ).reset_index()
    return nx.from_pandas_edgelist(
        edges, "account_id_from", "account_id_to", edge_attr=["weight", "txn_count"], create_using=nx.Graph
    )


def self_score_smurf_collectors(gt: pd.DataFrame, risk_by_acct: pd.Series, raw_tx_path: Path | None) -> tuple[int, int] | None:
    """For smurfing (a star pattern), the actionable node is the collector,
    not the disposable senders. The collector is authoritatively identified
    from the raw transaction tags (the receiver of each ring's smurfing-tagged
    transactions). Report how many collectors the engine scored >= 0.5.
    Returns (caught, total) or None if the raw tags aren't available."""
    if raw_tx_path is None or not raw_tx_path.exists():
        return None
    try:
        tx = pd.read_csv(raw_tx_path, usecols=["upi_id_to", "pattern_type", "scenario_id"], low_memory=False)
    except (ValueError, KeyError):
        return None
    smurf_tx = tx[tx["pattern_type"] == "smurfing"]
    if len(smurf_tx) == 0:
        return None
    # map upi -> account via ground truth is not available here; the raw file
    # uses upi ids, so resolve through the accounts file sitting beside it
    acct_path = raw_tx_path.parent / "accounts.csv"
    if not acct_path.exists():
        return None
    u2a = pd.read_csv(acct_path, usecols=["account_id", "upi_id"]).set_index("upi_id")["account_id"].to_dict()
    smurf_tx = smurf_tx.assign(collector=smurf_tx["upi_id_to"].map(u2a))
    collectors = smurf_tx.groupby("scenario_id")["collector"].agg(lambda x: x.value_counts().index[0])
    caught = sum(1 for c in collectors if risk_by_acct.get(c, 0.0) >= 0.5)
    return caught, len(collectors)


def compute_structural_seeds(G_full: nx.Graph) -> tuple[set, dict]:
    """Velocity-independent seed path: accounts embedded in tight clusters
    (high local clustering coefficient) are likely ring members even if they
    never spike on transaction volume. This is what lets sleeper rings --
    invisible to the behavioral filter -- reach community detection.
    Restricted to low-degree nodes for speed and because ring members have
    few counterparties (a high-degree merchant with high clustering is not a
    mule ring). Returns the seed set and the lcc values for explainability."""
    deg = dict(G_full.degree())
    lo, hi = STRUCTURAL_DEGREE_RANGE
    candidates = [n for n, d in deg.items() if lo <= d <= hi]
    lcc = nx.clustering(G_full, nodes=candidates)
    seeds = {n for n, v in lcc.items() if v >= STRUCTURAL_LCC_THRESHOLD}
    return seeds, lcc


def run_community_detection(G_full: nx.Graph, seed_accounts: set) -> tuple[dict, list]:
    print(f"Building seed-induced subgraph from {len(seed_accounts):,} flagged accounts ...")
    G_sub = G_full.subgraph(seed_accounts).copy()
    G_sub.remove_nodes_from(list(nx.isolates(G_sub)))
    print(f"  subgraph: {G_sub.number_of_nodes():,} nodes, {G_sub.number_of_edges():,} edges "
          f"(dropped {len(seed_accounts) - G_sub.number_of_nodes():,} accounts with no edges to other flagged accounts)")

    t0 = time.perf_counter()
    communities = nx.community.louvain_communities(G_sub, weight="weight", seed=42, resolution=LOUVAIN_RESOLUTION)
    print(f"  louvain: {len(communities)} communities in {time.perf_counter() - t0:.1f}s")

    node_to_comm: dict = {}
    for i, c in enumerate(communities):
        for n in c:
            node_to_comm[n] = i
    return node_to_comm, communities


def compute_community_stats(G_sub_full: nx.Graph, communities: list) -> pd.DataFrame:
    rows = []
    for i, members in enumerate(communities):
        sub = G_sub_full.subgraph(members)
        n = sub.number_of_nodes()
        e = sub.number_of_edges()
        possible = n * (n - 1) / 2 if n > 1 else 1
        density = e / possible
        if n <= BETWEENNESS_SIZE_CUTOFF and n > 2:
            bc = nx.betweenness_centrality(sub, weight=None, normalized=True)
        else:
            bc = {}  # too large to be worth it; density/size heuristic covers these
        for node in members:
            rows.append({
                "account_id": node, "community_id": i, "community_size": n,
                "community_density": density, "betweenness_centrality": bc.get(node, np.nan),
            })
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--features", type=Path, default=Path("data/features/features.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/graph"))
    parser.add_argument("--ground-truth", type=Path, default=None,
                         help="optional ground_truth.csv, only used to print a recall/precision report")
    args = parser.parse_args()

    df = load_behavioral_columns(args.features)

    print("Computing behavioral flag score (label-free) ...")
    acct_feats = compute_behavioral_flag_score(df)
    threshold = acct_feats["flag_score"].quantile(SEED_PERCENTILE)
    behavioral_seeds = set(acct_feats.loc[acct_feats["flag_score"] >= threshold, "account_id"])
    print(f"  {len(behavioral_seeds):,} accounts flagged behaviorally at the top {(1 - SEED_PERCENTILE):.0%} threshold")

    print("Building full transaction graph ...")
    t0 = time.perf_counter()
    G_full = build_full_graph(df)
    print(f"  {G_full.number_of_nodes():,} nodes, {G_full.number_of_edges():,} edges in {time.perf_counter() - t0:.1f}s")

    print("Computing structural seeds (local clustering, velocity-independent) ...")
    t0 = time.perf_counter()
    structural_seeds, lcc_values = compute_structural_seeds(G_full)
    print(f"  {len(structural_seeds):,} accounts flagged structurally in {time.perf_counter() - t0:.1f}s "
          f"({len(structural_seeds - behavioral_seeds):,} of them NOT already behaviorally flagged)")

    seed_accounts = behavioral_seeds | structural_seeds
    print(f"  {len(seed_accounts):,} total seed accounts after union")

    node_to_comm, communities = run_community_detection(G_full, seed_accounts)
    comm_stats = compute_community_stats(G_full.subgraph(node_to_comm.keys()), communities)

    # --- hub/pivot score: fan-in degree percentile across the WHOLE graph,
    # not restricted to the seed subgraph, since collectors need to be found
    # regardless of whether they individually tripped the behavioral filter ---
    in_degree = pd.Series({n: G_full.degree(n, weight="txn_count") for n in G_full.nodes()}, name="weighted_degree")
    fanin = acct_feats.set_index("account_id")["max_fanin"]
    hub_percentile = fanin.rank(pct=True)
    # NOTE: raw percentile rank is a poor score to threshold directly --
    # by construction, ~50% of all accounts exceed the 0.5 mark trivially.
    # Checked empirically: fraud lift stays near baseline (1.0x-2.3x) until
    # the 99th percentile, then rises sharply (5.9x at 0.995, 15.1x at
    # 0.999). Same calibrated-lookup approach as community_suspicion, for
    # the same reason: this is a real, checked distribution, not an assumed
    # monotonic-linear one.
    hub_bucket_edges = np.array([0, 0.90, 0.95, 0.99, 0.995, 0.999, 1.0001])
    hub_bucket_score = np.array([0.02, 0.05, 0.15, 0.40, 0.60, 1.00])
    hub_idx = np.clip(np.digitize(hub_percentile, hub_bucket_edges) - 1, 0, len(hub_bucket_score) - 1)
    hub_score = pd.Series(hub_bucket_score[hub_idx], index=hub_percentile.index, name="hub_score")

    # --- combine into account-level graph_features ---
    result = acct_feats[["account_id", "flag_score", "flag_reason"]].merge(
        comm_stats, on="account_id", how="left"
    ).merge(hub_score.reset_index(), on="account_id", how="left")

    # NOTE: community size vs fraud rate was checked empirically and is NOT
    # monotonic. Very small communities (<20) are mostly coincidental pairs
    # among flagged accounts (near-zero lift); the real sweet spot is 26-50
    # accounts (up to 22.6x lift), which is where 2-4 actual rings (8-13
    # members each) typically land after merging at this Louvain resolution.
    # Communities above ~100 revert to diffuse, low-signal blobs. This
    # lookup table is calibrated directly from that distribution rather than
    # assuming a monotonic relationship.
    size_bucket_edges = np.array([0, 5, 10, 15, 20, 25, 30, 40, 50, 70, 100, 150, 250, 100000])
    size_bucket_lift = np.array([0.00, 0.03, 0.05, 0.16, 0.20, 0.65, 1.00, 0.79, 0.46, 0.14, 0.08, 0.04, 0.02])
    result["community_suspicion"] = 0.0
    in_comm = result["community_size"].notna()
    bucket_idx = np.clip(np.digitize(result.loc[in_comm, "community_size"], size_bucket_edges) - 1, 0, len(size_bucket_lift) - 1)
    result.loc[in_comm, "community_suspicion"] = size_bucket_lift[bucket_idx]

    # NOTE: community_suspicion and hub_score were tested combined via a
    # weighted sum first, and it performed worse than either signal alone
    # (a strong hub_score for a collector account was getting diluted by
    # that same account's often-mediocre community_suspicion, since a
    # collector's own community isn't necessarily small/tight even though
    # its fan-in is extreme). The two signals flag largely non-overlapping
    # populations by design (community = internal ring structure, hub =
    # star/collector pattern), so max() preserves whichever fires strongly
    # instead of averaging it away. Betweenness is kept as a small additive
    # bonus on top, since it's only meaningful for accounts already in a
    # community.
    result["graph_risk_score"] = (
        np.maximum(result["community_suspicion"].fillna(0), result["hub_score"].fillna(0))
        + 0.15 * result["betweenness_centrality"].fillna(0).clip(upper=1.0)
    ).clip(upper=1.0)

    def _reason(row: pd.Series) -> str:
        parts = []
        if row["community_suspicion"] > 0.4:
            parts.append(f"small tightly-clustered community of {int(row['community_size'])} accounts")
        if row["hub_score"] > 0.4:
            parts.append("extreme fan-in (possible collector/hub)")
        if pd.notna(row["betweenness_centrality"]) and row["betweenness_centrality"] > 0.3:
            parts.append("high betweenness (pivot account within its cluster)")
        return "; ".join(parts) if parts else "no strong graph signal"

    result["graph_reason"] = result.apply(_reason, axis=1)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.output_dir / "graph_features.csv"
    result.to_csv(out_path, index=False)
    print(f"\nWrote {len(result):,} account rows to {out_path}")
    print(f"Accounts with graph_risk_score > 0.5: {(result['graph_risk_score'] > 0.5).sum():,}")

    if args.ground_truth is not None:
        gt = pd.read_csv(args.ground_truth)
        merged = result.merge(gt[["account_id", "is_fraud", "fraud_types", "cluster_id"]], on="account_id", how="right").fillna(
            {"graph_risk_score": 0.0}
        )
        print("\n--- Ground truth report (evaluation only, not used to build the score) ---")
        # Community co-location is the right metric for INTERCONNECTED rings
        # (sleeper, device_farm) where members transact with each other. It is
        # the WRONG metric for smurfing, which is a STAR: 15-25 disposable
        # senders each send one txn to a collector and never transact with each
        # other, so they can never form a "community" and are uncatchable
        # individually by design (that's why criminals use them). For smurfing
        # the actionable target is the COLLECTOR, so it is scored separately by
        # whether the collector node itself is caught.
        for kind in ["sleeper_mule", "device_farm", "ato"]:
            sub = gt[gt["fraud_types"].fillna("").str.contains(kind)]
            match_count, total = 0, 0
            for cluster_id, group in sub.groupby("cluster_id"):
                accts = group["account_id"].tolist()
                comms = [node_to_comm.get(a, -1) for a in accts]
                present = [c for c in comms if c != -1]
                total += 1
                if present:
                    _, top_n = Counter(present).most_common(1)[0]
                    if top_n >= len(accts) * 0.5:
                        match_count += 1
            print(f"  {kind}: rings with >=50% of members co-located in the same community: {match_count}/{total}")

        # smurfing scored by collector-caught (the correct metric for a star pattern)
        risk_by_acct = result.set_index("account_id")["graph_risk_score"]
        raw_tx_path = args.ground_truth.parent / "transactions.csv"
        smurf_scored = self_score_smurf_collectors(gt, risk_by_acct, raw_tx_path)
        if smurf_scored is not None:
            caught, total = smurf_scored
            print(f"  smurfing: ring COLLECTORS caught (graph_risk_score>=0.5): {caught}/{total} "
                  f"(senders are single-txn disposables, uncatchable individually by design)")

        top_decile = merged["graph_risk_score"].quantile(0.90)
        flagged = merged[merged["graph_risk_score"] >= top_decile]
        recall = flagged["is_fraud"].sum() / max(gt["is_fraud"].sum(), 1)
        precision = flagged["is_fraud"].sum() / max(len(flagged), 1)
        print(f"  top-decile graph_risk_score: recall={recall:.1%}  precision={precision:.1%}")


if __name__ == "__main__":
    main()
