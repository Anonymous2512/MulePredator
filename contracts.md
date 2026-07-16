# MulePredator API Contracts

Reference for `api/main.py` — every endpoint the FastAPI service exposes,
what it expects, and exactly what it returns. Written so the dashboard can
be redesigned from scratch against this contract without re-reading the
backend source.

Base URL (as run per the README): `http://localhost:8000`
Interactive docs (auto-generated from these same schemas): `/docs`

CORS is wide open (`allow_origins=["*"]`) — any origin can call this API,
no auth. Fine for a hackathon demo, not for production.

---

## Startup behavior

On boot, `main.py` loads `data/graph/graph_features.csv` (fails hard if
missing — the API will not start without it) and, if present,
`data/features/features.csv` (used only to precompute a volume-percentile
table for the quantum score). It then builds one in-process `RealtimeScorer`
that holds all rolling per-account state. **State is not persisted or
shared across workers** — restarting the process, or running with multiple
uvicorn workers, resets/fragments memory of recent transactions.

Two in-process ring buffers back the read endpoints:
- `recent_feed` — last **100** scored transactions, clean + flagged, newest first.
- `recent_alerts` — last **500** flagged (`alert == true`) transactions, newest first.

---

## `POST /score`

Score one transaction in real time (<1ms typical). This is the only
write/mutating endpoint — every call updates rolling state, counters, and
both ring buffers.

### Request body (`TxnRequest`)

| field | type | required | default | notes |
|---|---|---|---|---|
| `txn_id` | string | yes | — | |
| `timestamp` | ISO datetime | yes | — | |
| `account_id_from` | string | yes | — | sender |
| `account_id_to` | string | yes | — | receiver |
| `amount_inr` | float | yes | — | must be `> 0` |
| `auth_success` | bool | no | `true` | |
| `is_new_device` | bool | no | `false` | |
| `device_id` | string | no | `""` | |
| `ip_address` | string | no | `""` | |
| `impossible_travel` | bool | no | `false` | |
| `tls_version` | string | no | `"TLSv1.3"` | e.g. `TLSv1.0` / `TLSv1.2` / `TLSv1.3` |
| `key_size_bits` | int \| null | no | `null` | e.g. `1024` / `2048` |
| `is_forward_secret` | bool | no | `true` | |
| `is_pqc_protected` | bool | no | `true` | |
| `data_volume_mb` | float | no | `0.0` | |
| `hndl_risk` | bool | no | `false` | "harvest now, decrypt later" flag |

### Response body

The scorer's raw output dict, with four request-derived fields merged in.
All fields present on every response:

| field | type | meaning |
|---|---|---|
| `txn_id` | string | echoed |
| `account_id` | string | == `account_id_from` |
| `account_id_from` | string | echoed |
| `account_id_to` | string | echoed |
| `amount_inr` | float | echoed |
| `timestamp` | ISO string | echoed |
| `decision` | `"allow"` \| `"step_up"` \| `"block"` | `block` if `alert_tier == "high_priority"`, `step_up` if `alert` (any tier), else `allow` |
| `alert` | bool | true if convergence (2+ signals) **or** a critical single-engine override fired |
| `alert_tier` | `"none"` \| `"monitor"` \| `"investigate"` \| `"high_priority"` | triage tier |
| `trigger_type` | `"None"` \| `"Converged"` \| `"Critical Single-Engine Override"` | **why** it alerted — new field, see below |
| `fraud_score` | float 0-1 | `max(graph_risk_score, cyber_risk_score)` |
| `n_fraud_signals` | int 0-2 | how many of {graph, cyber} independently fired (score >= 0.3) |
| `graph_risk_score` | float 0-1 | cached, from the offline graph engine |
| `cyber_risk_score` | float 0-1 | live-computed (impossible travel, device/IP churn, failed logins) |
| `quantum_exposure_score` | float 0-1 | live-computed, separate risk axis (not folded into `fraud_score`) |
| `quantum_alert` | bool | `quantum_exposure_score >= 0.6`, independent of `alert` |
| `reason` | string | human-readable explanation, e.g. `"CONVERGED (2 independent signals) -- cyber: ...; graph: ..."` or `"CRITICAL OVERRIDE (single-engine score >= 0.9) -- cyber: ..."` |
| `cluster_details` | object \| null | populated when the **receiving** account (`account_id_to`) looks like a live fan-in collector (graph-flagged + 3+ distinct senders in its trailing window); see shape below |
| `latency_ms` | float | scoring time for this call |

`cluster_details` shape (when non-null):
```json
{
  "collector_account_id": "string",
  "collector_graph_risk_score": 0.0,
  "sender_account_ids": ["string", "..."],
  "spokes": [
    {"account_id": "string", "amount_inr": 0.0, "timestamp": "ISO string"}
  ]
}
```

### `trigger_type` — new field (Smart Bypass)

Added to fix the "convergence trap": previously `alert` required 2+
independent signals, which suppressed extreme single-engine threats (e.g.
an account-takeover that only trips the cyber engine, or a smurfing ring
that only trips the graph engine). Now:

```
converged        = fraud_score >= 0.5 AND n_fraud_signals >= 2
critical_override = cyber_risk_score >= 0.90 OR graph_risk_score >= 0.90
alert             = converged OR critical_override
trigger_type      = "Converged"                          if converged
                    "Critical Single-Engine Override"     elif critical_override
                    "None"                                otherwise
```

Measured on the current synthetic dataset: 32,255 additional alerts now
fire under `"Critical Single-Engine Override"` that the old 2-signal-only
rule would have suppressed, vs. 2,803 under `"Converged"`.

### Errors
- `503` if the scorer hasn't finished loading yet (startup race).
- `422` (FastAPI default) on a malformed body / failed `amount_inr > 0` validation.

---

## `GET /feed?limit=100`

Rolling window of **every** scored transaction (clean and flagged), newest
first — what the dashboard's live queue polls to animate the feed.

- Query param `limit` (int, default `100`) — truncates the returned list;
  does not affect how many are stored (buffer max is always 100).
- Response: `{"count": <int>, "feed": [<same shape as /score response>, ...]}`

---

## `GET /alerts?limit=50&tier=<string>`

Recent **flagged** transactions only (`alert == true`), newest first.

- Query param `limit` (int, default `50`).
- Query param `tier` (optional string) — filters to one of
  `"monitor" | "investigate" | "high_priority"` (exact match on `alert_tier`).
- Response: `{"count": <int>, "alerts": [<same shape as /score response>, ...]}`

Note: `count` reflects the *post-truncation* length (`len(items[:limit])`),
not the total number of matching alerts in the buffer.

---

## `GET /account/{account_id}`

Cached graph intelligence + live rolling state for one account — what the
dashboard shows when an analyst drills into an account.

Response:
```json
{
  "account_id": "string",
  "graph_risk_score": 0.0,
  "graph_reason": "string",
  "recent_txn_count": 0,
  "recent_inbound_count": 0,
  "recent_auth_count": 0
}
```
If the account has no rolling state yet (never seen by this process), the
three `recent_*` counts are all `0` rather than an error. Errors: `503` if
the scorer isn't loaded.

---

## `GET /stats`

Running counters since process start.

Response:
```json
{
  "transactions_scored": 0,
  "fraud_alerts": 0,
  "quantum_alerts": 0,
  "alert_rate": 0.0,
  "avg_latency_ms": 0.0
}
```
`alert_rate` = `fraud_alerts / transactions_scored` (0 if none scored yet).

---

## `GET /health`

Liveness probe.

Response:
```json
{"status": "ok", "scorer_loaded": true, "scored": 0}
```

---

## Redesign notes for the frontend

If rebuilding the dashboard from scratch against this contract:
- `/feed` is the primary live-polling seam (all traffic, for the main
  queue/ticker); `/alerts` is a separate, smaller, pre-filtered stream —
  don't conflate them, they serve different UI regions.
- The "Suspicious vs Flagged" split the current dashboard draws is a
  **derived** UI concept, not an API field: `n_fraud_signals == 1` reads as
  "suspicious", `>= 2` (or now, `critical_override == true`) reads as
  "flagged". `alert_tier` is a separate, secondary triage axis — a redesign
  can choose to surface one, both, or neither as the primary sort key.
  `trigger_type` is now the cleanest single field for "why did this fire" —
  consider making it the primary badge/axis instead of re-deriving it from
  `n_fraud_signals`.
  `cluster_details` is only ever present on the transaction whose
  **receiver** is a fan-in collector — never key network-view rendering off
  `account_id_from`.
- Every numeric score field is already 0-1 and pre-rounded server-side
  (4 decimal places) — no client-side normalization needed.
- No pagination beyond `limit` — the server-side buffers are hard-capped
  (100 for `/feed`, 500 for `/alerts`), so "load more" beyond that requires
  a backend change (e.g. a real DB), not a bigger `limit`.
