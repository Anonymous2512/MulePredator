# MulePredator — What This Repo Actually Is

Real-time UPI fraud/mule detection system + SOC dashboard. Core thesis: don't
alert on single weak signals, alert when **independent engines converge** on
the same account/transaction.

## Current pieces

**Offline (batch)** — `data-generator/generate.py` → `build_feature_table.py`
→ synthesizes fake UPI transaction/auth/TLS data (50k accounts, 2M txns,
injected fraud rings: sleeper mules, smurfing, device farms, ATO, HNDL
crypto-harvest accounts). `config.yaml` controls all of it.

**Engines** (`engines/`) — each reads `features.csv`, writes a risk score:
- `graph_engine.py` — Louvain communities, fan-in/fan-out, hub centrality
- `cyber_engine.py` — impossible travel, device churn, failed-login bursts
- `quantum_engine.py` — "harvest now decrypt later" exposure (weak TLS/RSA
  key size, no forward secrecy)
- `fusion_engine.py` — convergence layer: 2+ engines agreeing = high priority

**Online (real-time)** — `api/main.py` + `api/realtime_scorer.py`. FastAPI
service, loads cached graph scores at startup, scores incoming transactions
in <1ms (measured ~0.01-0.05ms, ~21k txn/s single-threaded). `api/replay.py`
streams the dataset at the API to simulate live traffic.

**Endpoints today**: `/score` (POST single txn), `/alerts` (recent flagged),
`/account/{id}`, `/stats`, `/health`. No `/feed` endpoint yet — that's the gap.

**Dashboard** — `dashboard/mulepredator_dashboard.html` (+ a React
`.jsx` twin). SOC console: transaction queue, alert tiers, a Network View SVG
that is currently a **static mock ring**, not driven by real cluster data.

## What `to-fix.md` is asking for (not yet built)

1. Two-tier filter UI: primary (All/Suspicious/Flagged by signal count) ×
   secondary (All/High/Inv/Mon tier) — dashboard-only change.
2. Backend: attach a `cluster_details` object (hub account + spoke sender
   accounts) to `/score` responses when the graph engine flags smurfing, so
   the frontend can replace the static ring with real fan-in data.
3. New `/feed` endpoint: rolling window of last 100 transactions (clean +
   flagged) so the dashboard has something to poll besides `/alerts`
   (which only holds flagged ones).
4. Point dashboard's `USE_LIVE_API` polling at `/feed`.

This is the immediate, well-scoped next task — worth doing first since
everything below builds on having real cluster data flowing to the UI.

## Ideas beyond to-fix.md

**Near-term, cheap, same architecture:**
- Redis-backed rolling state instead of in-process `deque`/dict — `api/README.md`
  already flags this as the production gap; unlocks horizontal scaling and
  survives restarts.
- `/feed` should support cursor/since-id pagination, not just "last 100" —
  a dashboard polling every second will re-fetch overlapping windows otherwise.
- WebSocket push (`/ws/feed`) instead of polling — dashboard already polls on
  an interval; a socket removes the latency/bandwidth tradeoff entirely and is
  a natural fit for a "live feed" SOC console.
- Feedback loop: let an analyst mark an alert "confirmed fraud" / "false
  positive" from the dashboard, persist it, and use it to tune fusion
  thresholds over time — right now convergence weights in `fusion_engine.py`
  are static.
- Case management: group alerts by cluster/ring instead of showing them as
  isolated transactions — an analyst investigating a smurfing ring today has
  to manually correlate multiple queue entries.

**Medium-term, extends detection surface:**
- Time-decayed graph scores: currently `graph_features.csv` is a batch
  snapshot refreshed "hourly" per the README note — a scheduled reload
  endpoint (`POST /reload-graph`) would close that gap without a restart.
  Explore in AskUserQuestion-tier is unnecessary here, just direct: worth doing.
- Explainability panel: `reason` string exists in `/score` responses already
  — surface it more prominently per-alert in the dashboard instead of only in
  raw payload, so analysts don't need `/docs` to see why something fired.
- Synthetic data validation dashboard: `config.yaml`'s `validation` block
  tracks fraud prevalence ranges — worth a small script/report comparing
  each `generate.py` run's actual output against those bounds, catching
  config drift before it silently changes label quality.

**Bigger bets, worth a real discussion before building:**
- Replace the static synthetic replay with a pluggable ingestion adapter
  (Kafka/webhook) so this could sit in front of a real transaction stream
  instead of only `replay.py`.
- Multi-tenant / bank-boundary awareness — right now everything is one flat
  account space; real UPI fraud rings often span PSPs.
- Model-based fusion instead of rule-based convergence counting — once
  there's labeled analyst feedback (see feedback loop above), a learned
  fusion layer could outperform "2+ engines agree."

## Suggested order

1. Ship the three to-fix.md items — they're scoped, backend-driven, and
   the dashboard already expects them (`USE_LIVE_API` flag exists, just
   points nowhere useful yet).
2. Redis state + `/feed` pagination — same surface area, prevents rework.
3. WebSocket push — natural next step once `/feed` shape is stable.
4. Everything else is optional depth, pick based on whether this is headed
   toward a demo, a hackathon submission, or a real deployment — that
   changes the priority a lot (demo → dashboard polish; real deployment →
   Redis/Kafka/case management first).
