#!/usr/bin/env python3
"""
realtime_scorer.py

Real-time single-transaction scorer. This is the ONLINE half of the
architecture; the batch engines are the OFFLINE half. The split is
deliberate and matches the system design: compute-heavy graph analysis
(Louvain communities, centrality) is precomputed periodically and CACHED as
per-account graph_risk_score, while the fast, per-transaction signals
(trailing behavior, cyber rules, quantum crypto posture) are computed live
when a transaction arrives.

The scoring LOGIC here is deliberately identical to the batch engines'
thresholds and fusion so that live decisions match what was validated
offline. Where a constant is shared it is imported/duplicated with a note.

State model:
  - graph_scores: dict account_id -> graph_risk_score (loaded from the
    cached graph_features.csv the batch job produces; refreshed out of band)
  - rolling per-account deques of recent (timestamp, amount, counterparty)
    transactions and recent (timestamp, success, is_new_device, device_id,
    ip, impossible_travel) auth events, trimmed to the trailing window

A production version would back the rolling state with Redis (as the deck's
architecture shows); an in-memory dict is used here so the demo runs with no
external services.

KNOWN PARITY LIMITATION (be honest about this): the real-time cyber score
can differ slightly from the batch cyber_engine output (~4% of transactions
by >0.15, alert agreement ~99.75% on a 50k replay). Two causes, both benign:
(1) the batch feature builder computes trailing auth stats from the FULL
auth-event stream, which includes logins not tied to a transaction; the
real-time scorer only sees auth context that arrives attached to a
transaction, so its rolling auth window is slightly sparser. (2) window
warm-up: an account's first transactions in a replay have less trailing
history than the batch job (which saw all history) computed for them. Neither
is a logic error; feeding the scorer the full auth stream (not just
transaction-attached auth) would close the gap, and is the right production
design.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# --- constants duplicated from the batch engines (keep in sync) ---
WINDOW = timedelta(hours=6)

# cyber (from cyber_engine.py, post-leakage-fix calibration)
FAILED_LOGIN_THRESHOLD = 2
IMPOSSIBLE_TRAVEL_THRESHOLD = 2
NEW_DEVICE_THRESHOLD = 2
DEVICE_CHURN_THRESHOLD = 3
IP_CHURN_THRESHOLD = 3

# quantum (from quantum_engine.py)
TLS_SEVERITY = {"TLSv1.0": 1.0, "TLSv1.2": 0.6, "TLSv1.3": 0.2}
KEY_SIZE_SEVERITY = {1024: 1.0, 2048: 0.6}

# fusion (from fusion_engine.py)
SIGNAL_PRESENCE_THRESHOLD = 0.3
ALERT_SCORE_THRESHOLD = 0.5
CRITICAL_SCORE_THRESHOLD = 0.90   # single-engine score that bypasses convergence
QUANTUM_EXPOSURE_ALERT = 0.6

# cluster view: minimum distinct senders in the trailing window before a
# collector's inbound fan-in is worth rendering as a live cluster
HUB_FANIN_MIN = 3


@dataclass
class Transaction:
    txn_id: str
    timestamp: datetime
    account_id_from: str
    account_id_to: str
    amount_inr: float
    # auth context for this transaction
    auth_success: bool = True
    is_new_device: bool = False
    device_id: str = ""
    ip_address: str = ""
    impossible_travel: bool = False
    # crypto context for this transaction
    tls_version: str = "TLSv1.3"
    key_size_bits: int | None = None
    is_forward_secret: bool = True
    is_pqc_protected: bool = True
    data_volume_mb: float = 0.0
    hndl_risk: bool = False


@dataclass
class AccountState:
    txns: deque = field(default_factory=deque)      # (ts, amount, counterparty)
    auths: deque = field(default_factory=deque)      # (ts, success, is_new_device, device_id, ip, impossible_travel)
    inbound: deque = field(default_factory=deque)    # (ts, amount, counterparty) received


class RealtimeScorer:
    def __init__(self, graph_scores: dict[str, float], graph_reasons: dict[str, str] | None = None,
                 volume_percentiles: list[float] | None = None):
        self.graph_scores = graph_scores
        self.graph_reasons = graph_reasons or {}
        self.state: dict[str, AccountState] = defaultdict(AccountState)
        # data-volume percentile breakpoints for the quantum volume_rank, taken
        # from the batch dataset so the online rank matches offline
        self.volume_percentiles = volume_percentiles or []

    # ---- state maintenance ----
    def _trim(self, dq: deque, now: datetime) -> None:
        cutoff = now - WINDOW
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _update_state(self, txn: Transaction) -> None:
        s = self.state[txn.account_id_from]
        s.txns.append((txn.timestamp, txn.amount_inr, txn.account_id_to))
        s.auths.append((txn.timestamp, txn.auth_success, txn.is_new_device,
                        txn.device_id, txn.ip_address, txn.impossible_travel))
        self._trim(s.txns, txn.timestamp)
        self._trim(s.auths, txn.timestamp)
        # receiver's inbound state (for fan-in / collector detection)
        r = self.state[txn.account_id_to]
        r.inbound.append((txn.timestamp, txn.amount_inr, txn.account_id_from))
        self._trim(r.inbound, txn.timestamp)

    # ---- signal computation (mirrors batch engines) ----
    def _cyber_score(self, txn: Transaction) -> tuple[float, list[str]]:
        s = self.state[txn.account_id_from]
        auths = s.auths
        failed = sum(1 for a in auths if not a[1])
        new_dev = sum(1 for a in auths if a[2])
        imp_travel = sum(1 for a in auths if a[5]) + (1 if txn.impossible_travel else 0)
        distinct_dev = len({a[3] for a in auths if a[3]})
        distinct_ip = len({a[4] for a in auths if a[4]})

        reasons = []
        hard = 0.0
        if failed >= FAILED_LOGIN_THRESHOLD:
            hard = max(hard, 1.0); reasons.append("failed login burst")
        if imp_travel >= IMPOSSIBLE_TRAVEL_THRESHOLD or txn.impossible_travel:
            hard = max(hard, 1.0); reasons.append("impossible travel velocity")
        if new_dev >= NEW_DEVICE_THRESHOLD:
            hard = max(hard, 0.6); reasons.append("new device")
        if distinct_dev >= DEVICE_CHURN_THRESHOLD or distinct_ip >= IP_CHURN_THRESHOLD:
            hard = max(hard, 1.0); reasons.append("device/IP churn")

        soft = 0.0
        if distinct_dev == 2: soft += 0.25
        if distinct_ip == 2: soft += 0.25
        if failed == 1: soft += 0.20
        if new_dev == 1: soft += 0.15
        soft = min(soft, 0.5)

        score = min(max(hard, soft), 1.0)
        return score, reasons

    def _quantum_score(self, txn: Transaction) -> tuple[float, str]:
        if not txn.hndl_risk:
            return 0.0, "modern crypto (PQC or forward-secret) -- not exposed"
        tls_sev = TLS_SEVERITY.get(txn.tls_version, 0.2)
        key_sev = KEY_SIZE_SEVERITY.get(txn.key_size_bits, 0.0) if txn.key_size_bits else 0.0
        fs_sev = 0.0 if txn.is_forward_secret else 1.0
        severity = min(0.4 * tls_sev + 0.35 * key_sev + 0.25 * fs_sev, 1.0)
        # volume rank vs cached percentiles
        vol_rank = 0.5
        if self.volume_percentiles:
            import bisect
            vol_rank = bisect.bisect_left(self.volume_percentiles, txn.data_volume_mb) / len(self.volume_percentiles)
        # trailing weak-crypto rate (approximate from recent txns' hndl flags not tracked here;
        # use this txn as the driver, matching batch's per-txn gate)
        score = min(0.7 * severity + 0.3 * vol_rank, 1.0)
        reason = f"{txn.tls_version}"
        if txn.key_size_bits:
            reason += f", {txn.key_size_bits}-bit key"
        if not txn.is_forward_secret:
            reason += ", no forward secrecy"
        reason += f", {txn.data_volume_mb:.1f}MB transferred"
        return score, reason

    def _cluster_details(self, txn: Transaction) -> dict[str, Any] | None:
        """If the RECEIVER of this transaction looks like a fan-in hub
        (collector), return the real senders (spokes) currently in its
        trailing window so the dashboard can render the actual mule cluster
        instead of a static mock ring."""
        hub_id = txn.account_id_to
        hub_score = self.graph_scores.get(hub_id, 0.0)
        hub_reason = self.graph_reasons.get(hub_id, "")
        if hub_score < SIGNAL_PRESENCE_THRESHOLD or "fan-in" not in hub_reason:
            return None
        r = self.state.get(hub_id)
        if r is None:
            return None
        spokes = [
            {"account_id": acct_id, "amount_inr": amt, "timestamp": ts.isoformat()}
            for ts, amt, acct_id in r.inbound
        ]
        sender_ids = sorted({s["account_id"] for s in spokes})
        if len(sender_ids) < HUB_FANIN_MIN:
            return None
        return {
            "collector_account_id": hub_id,
            "collector_graph_risk_score": round(hub_score, 4),
            "sender_account_ids": sender_ids,
            "spokes": spokes,
        }

    def score(self, txn: Transaction) -> dict[str, Any]:
        t0 = time.perf_counter()
        self._update_state(txn)

        graph_score = self.graph_scores.get(txn.account_id_from, 0.0)
        graph_reason = self.graph_reasons.get(txn.account_id_from, "no strong graph signal")
        cyber_score, cyber_reasons = self._cyber_score(txn)
        quantum_score, quantum_reason = self._quantum_score(txn)

        # fusion (mirrors fusion_engine.py exactly)
        fraud_score = max(graph_score, cyber_score)
        graph_fired = graph_score >= SIGNAL_PRESENCE_THRESHOLD
        cyber_fired = cyber_score >= SIGNAL_PRESENCE_THRESHOLD
        n_signals = int(graph_fired) + int(cyber_fired)
        converged = (fraud_score >= ALERT_SCORE_THRESHOLD) and (n_signals >= 2)
        critical_override = (cyber_score >= CRITICAL_SCORE_THRESHOLD) or (graph_score >= CRITICAL_SCORE_THRESHOLD)
        alert = converged or critical_override
        trigger_type = "Converged" if converged else "Critical Single-Engine Override" if critical_override else "None"
        quantum_alert = quantum_score >= QUANTUM_EXPOSURE_ALERT

        tier = "none"
        if fraud_score >= ALERT_SCORE_THRESHOLD:
            tier = "monitor"
        if alert:
            tier = "investigate"
        if alert and fraud_score >= 0.8:
            tier = "high_priority"

        # reason assembly
        parts = []
        if cyber_fired:
            parts.append("cyber: " + ("; ".join(cyber_reasons) if cyber_reasons else f"elevated ({cyber_score:.2f})"))
        if graph_fired:
            parts.append(f"graph: {graph_reason}")
        if not parts:
            reason = "no fraud signals converged"
        elif converged:
            reason = f"CONVERGED ({n_signals} independent signals) -- " + "; ".join(parts)
        elif critical_override:
            reason = f"CRITICAL OVERRIDE (single-engine score >= {CRITICAL_SCORE_THRESHOLD}) -- " + "; ".join(parts)
        else:
            reason = "single signal only -- " + "; ".join(parts)
        if quantum_alert:
            reason += f" | SEPARATE quantum exposure: {quantum_reason}"

        cluster_details = self._cluster_details(txn)

        latency_ms = (time.perf_counter() - t0) * 1000
        return {
            "txn_id": txn.txn_id,
            "account_id": txn.account_id_from,
            "cluster_details": cluster_details,
            "decision": "block" if tier == "high_priority" else "step_up" if alert else "allow",
            "alert": alert,
            "alert_tier": tier,
            "fraud_score": round(fraud_score, 4),
            "n_fraud_signals": n_signals,
            "trigger_type": trigger_type,
            "graph_risk_score": round(graph_score, 4),
            "cyber_risk_score": round(cyber_score, 4),
            "quantum_exposure_score": round(quantum_score, 4),
            "quantum_alert": quantum_alert,
            "reason": reason,
            "latency_ms": round(latency_ms, 3),
        }
