# MulePredator Scoring API

Real-time mule / collusive-fraud + quantum-exposure scoring for UPI transactions.

## Architecture

The system splits into an **offline** (batch) half and an **online** (real-time) half,
matching the design in the project deck ("separate real-time transaction scoring from
compute-heavy graph analysis"):

- **Offline / batch** (`generate.py` → `build_feature_table.py` → the three engines):
  computes the slow, compute-heavy graph intelligence (Louvain communities, hub/centrality
  scores) and writes it to `graph_features.csv`. Run periodically.
- **Online / real-time** (`realtime_scorer.py` + `main.py`): scores a single incoming
  transaction in well under 1 ms by combining the *cached* graph score with *live-computed*
  behavioral, cyber, and quantum signals, then applying the same fusion + convergence logic
  the batch pipeline was validated on.

## Setup

```bash
pip install -r requirements.txt
```

## Prerequisites

Run the batch pipeline first to produce the cached graph intelligence:

```bash
# from the project root
python3 data-generator/generate.py --config config.yaml
python3 data-generator/build_feature_table.py --input-dir data/final --output-dir data/features --config config.yaml
python3 engines/graph_engine.py   --features data/features/features.csv --output-dir data/graph
python3 engines/cyber_engine.py   --features data/features/features.csv --output-dir data/cyber
python3 engines/quantum_engine.py --features data/features/features.csv --output-dir data/quantum
python3 engines/fusion_engine.py  --graph data/graph/graph_features.csv --cyber data/cyber/cyber_features.csv \
    --quantum data/quantum/quantum_features.csv --features data/features/features.csv --output-dir data/fusion
```

## Run the API

```bash
export GRAPH_FEATURES=data/graph/graph_features.csv
export FEATURES=data/features/features.csv
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/docs for the interactive Swagger UI.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/score` | Score a single transaction; returns decision, tier, per-engine scores, `cluster_details`, reason, latency |
| GET | `/feed?limit=100` | Rolling window of the last 100 scored transactions (clean + flagged), newest first |
| GET | `/alerts?limit=50&tier=high_priority` | Recent *flagged* alerts only |
| GET | `/account/{account_id}` | Cached graph intelligence + live rolling state for one account |
| GET | `/stats` | Running counters: scored, alerts, avg latency |
| GET | `/health` | Liveness check |

`cluster_details` (on `/score` and `/feed` items) is non-null when the
transaction's **receiving** account looks like a fan-in collector: cached
`graph_risk_score` above the signal-presence threshold with a "fan-in"
graph reason, and 3+ distinct senders in its rolling window. It carries the
real `collector_account_id` and `sender_account_ids`/`spokes` (with
amounts) so the dashboard can render the actual mule cluster instead of a
mock ring.

### Example `/score` request

```json
{
  "txn_id": "txn-123",
  "timestamp": "2026-07-01T12:00:00",
  "account_id_from": "acct-A",
  "account_id_to": "acct-B",
  "amount_inr": 48000,
  "is_new_device": true,
  "tls_version": "TLSv1.0",
  "key_size_bits": 1024,
  "is_forward_secret": false,
  "hndl_risk": true,
  "data_volume_mb": 500
}
```

### Response

```json
{
  "decision": "block",
  "alert": true,
  "alert_tier": "high_priority",
  "fraud_score": 1.0,
  "n_fraud_signals": 2,
  "graph_risk_score": 1.0,
  "cyber_risk_score": 1.0,
  "quantum_exposure_score": 0.85,
  "quantum_alert": true,
  "reason": "CONVERGED (2 independent signals) -- cyber: failed login burst; ... | SEPARATE quantum exposure: TLSv1.0, 1024-bit key, ...",
  "latency_ms": 0.12
}
```

## Driving a live demo

With the API running, stream the dataset at it to simulate a live feed:

```bash
python3 replay.py --features data/features/features.csv --rate 50
```

`--rate` is transactions/second. Watch alerts stream in the API's `/alerts` feed or the dashboard.

## Measured performance

On a 50k-transaction replay (single process, in-memory state):

- **p50 latency: 0.010 ms, p95: 0.018 ms, p99: 0.05 ms** (well under the 100 ms design target)
- **throughput: ~21,000 txn/s** single-threaded
- alert decision agreement with the batch pipeline: **99.75%** (see the parity note in
  `realtime_scorer.py` for the small, benign differences)

## Notes for production

- Rolling per-account state is held in memory here; back it with Redis (as in the deck's
  architecture) for horizontal scaling and durability.
- Feed the scorer the full auth-event stream (not just transaction-attached auth) to close
  the small real-time/batch parity gap documented in `realtime_scorer.py`.
- Refresh the cached `graph_features.csv` on a schedule (e.g. hourly) as new transactions
  accumulate; the online path picks up the new cache on restart or via a reload endpoint.
