#!/usr/bin/env python3
"""
main.py -- FastAPI scoring service for the mule/fraud detection system.

Serves the ONLINE half of the architecture: a low-latency /score endpoint
that scores a single transaction in real time using cached graph
intelligence + live-computed behavioral/cyber/quantum signals, plus
supporting endpoints for the dashboard.

Run:
    pip install fastapi uvicorn pandas numpy
    uvicorn main:app --reload --port 8000

Then open http://localhost:8000/docs for the interactive API.

Environment:
    GRAPH_FEATURES  path to cached graph_features.csv (default data/graph/graph_features.csv)
    FEATURES        path to features.csv, used only to precompute volume percentiles
                    for the quantum score (default data/features/features.csv)
"""
from __future__ import annotations

import os
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from realtime_scorer import RealtimeScorer, Transaction

app = FastAPI(
    title="MulePredator Scoring API",
    description="Real-time mule / collusive-fraud + quantum-exposure scoring for UPI transactions",
    version="1.0",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# --- module state, initialized on startup ---
scorer: RealtimeScorer | None = None
recent_alerts: deque = deque(maxlen=500)   # ring buffer of recent alerts for the dashboard
stats = {"scored": 0, "alerts": 0, "quantum_alerts": 0, "latency_sum_ms": 0.0}


# ---- request/response schemas ----
class TxnRequest(BaseModel):
    txn_id: str
    timestamp: datetime
    account_id_from: str
    account_id_to: str
    amount_inr: float = Field(gt=0)
    auth_success: bool = True
    is_new_device: bool = False
    device_id: str = ""
    ip_address: str = ""
    impossible_travel: bool = False
    tls_version: str = "TLSv1.3"
    key_size_bits: int | None = None
    is_forward_secret: bool = True
    is_pqc_protected: bool = True
    data_volume_mb: float = 0.0
    hndl_risk: bool = False


@app.on_event("startup")
def _load() -> None:
    global scorer
    graph_path = Path(os.environ.get("GRAPH_FEATURES", "data/graph/graph_features.csv"))
    features_path = Path(os.environ.get("FEATURES", "data/features/features.csv"))

    if not graph_path.exists():
        raise RuntimeError(f"graph features not found at {graph_path}; run graph_engine.py first")
    g = pd.read_csv(graph_path)
    graph_scores = dict(zip(g["account_id"], g["graph_risk_score"]))
    graph_reasons = dict(zip(g["account_id"], g["graph_reason"].fillna("no strong graph signal")))

    vol_pcts: list[float] = []
    if features_path.exists():
        v = pd.read_csv(features_path, usecols=["crypto_data_volume_mb"], low_memory=False)
        vol_pcts = sorted(np.log1p(v["crypto_data_volume_mb"].fillna(0)).quantile(np.linspace(0, 1, 101)).tolist())

    scorer = RealtimeScorer(graph_scores, graph_reasons, vol_pcts)
    print(f"Loaded {len(graph_scores):,} cached graph scores; scorer ready.")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "scorer_loaded": scorer is not None, "scored": stats["scored"]}


@app.post("/score")
def score(txn: TxnRequest) -> dict[str, Any]:
    if scorer is None:
        raise HTTPException(503, "scorer not loaded")
    t = Transaction(
        txn_id=txn.txn_id, timestamp=txn.timestamp,
        account_id_from=txn.account_id_from, account_id_to=txn.account_id_to,
        amount_inr=txn.amount_inr, auth_success=txn.auth_success,
        is_new_device=txn.is_new_device, device_id=txn.device_id, ip_address=txn.ip_address,
        impossible_travel=txn.impossible_travel, tls_version=txn.tls_version,
        key_size_bits=txn.key_size_bits, is_forward_secret=txn.is_forward_secret,
        is_pqc_protected=txn.is_pqc_protected, data_volume_mb=txn.data_volume_mb, hndl_risk=txn.hndl_risk,
    )
    result = scorer.score(t)
    stats["scored"] += 1
    stats["latency_sum_ms"] += result["latency_ms"]
    if result["alert"]:
        stats["alerts"] += 1
        recent_alerts.appendleft(result)
    if result["quantum_alert"]:
        stats["quantum_alerts"] += 1
    return result


@app.get("/alerts")
def alerts(limit: int = 50, tier: str | None = None) -> dict[str, Any]:
    """Recent alerts for the dashboard feed, newest first."""
    items = list(recent_alerts)
    if tier:
        items = [a for a in items if a["alert_tier"] == tier]
    return {"count": len(items[:limit]), "alerts": items[:limit]}


@app.get("/account/{account_id}")
def account(account_id: str) -> dict[str, Any]:
    """Current cached graph intelligence + live rolling state for one account
    (what the dashboard shows when an analyst clicks an account)."""
    if scorer is None:
        raise HTTPException(503, "scorer not loaded")
    st = scorer.state.get(account_id)
    return {
        "account_id": account_id,
        "graph_risk_score": round(scorer.graph_scores.get(account_id, 0.0), 4),
        "graph_reason": scorer.graph_reasons.get(account_id, "no strong graph signal"),
        "recent_txn_count": len(st.txns) if st else 0,
        "recent_inbound_count": len(st.inbound) if st else 0,
        "recent_auth_count": len(st.auths) if st else 0,
    }


@app.get("/stats")
def get_stats() -> dict[str, Any]:
    avg_lat = stats["latency_sum_ms"] / stats["scored"] if stats["scored"] else 0.0
    return {
        "transactions_scored": stats["scored"],
        "fraud_alerts": stats["alerts"],
        "quantum_alerts": stats["quantum_alerts"],
        "alert_rate": round(stats["alerts"] / stats["scored"], 5) if stats["scored"] else 0.0,
        "avg_latency_ms": round(avg_lat, 4),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
