# MEMORY.md

Running memory for Claude sessions working in this repo. Not read by
humans as documentation (that's `README.md`/`PROJECT_OVERVIEW.md`), not
static rules (that's `CLAUDE.md`) — this is a log of what happened, what
was decided, and what future sessions should know before touching things
again. Append to it at the end of a session; don't rewrite history that's
already here, add new entries below.

Newest entries at the bottom.

---

## 2026-07-15 — to-fix.md items implemented (two-tier filter, dynamic cluster view, /feed)

Implemented all three items from `to-fix.md`:

1. **Two-tier filter**: dashboard primary filter simplified from 4 buttons
   (all/flagged/suspicious/alerts, overlapping semantics) to the exact 3
   the spec asked for: All / Suspicious (`n_fraud_signals == 1`) / Flagged
   (`n_fraud_signals >= 2`). Secondary tier filter (All/High/Inv/Mon) was
   already correct, left as-is.
2. **Dynamic network view**: added `cluster_details` to `RealtimeScorer.score()`
   output (`api/realtime_scorer.py`). It's populated when the transaction's
   **receiver** (not sender) looks like a fan-in hub — cached graph score
   above 0.3 with a "fan-in" graph_reason, and ≥3 distinct senders
   (`HUB_FANIN_MIN`) currently in that account's rolling `inbound` deque.
   Removed the dashboard's static `CLUSTERS` mock ring entirely; `NetworkView`
   now renders real sender/collector account IDs from `cluster_details`.
3. **`/feed` endpoint**: `api/main.py` now keeps `recent_feed` (deque,
   maxlen 100) storing *every* scored transaction (clean + flagged),
   distinct from `recent_alerts` (maxlen 500, flagged only). Dashboard's
   `USE_LIVE_API` poll switched from `/alerts` to `/feed?limit=100`.

**Why receiver, not sender, for cluster_details**: smurfing collectors
accumulate fan-in as the *receiving* account (`account_id_to`). The
existing fraud-score fusion (`fraud_score = max(graph_score, cyber_score)`)
only ever looks up `graph_scores[account_id_from]` — that's unrelated and
unchanged. `cluster_details` is a separate, additive lookup keyed on
`account_id_to`, computed regardless of whether the transaction alerts.
Don't conflate the two — a transaction can have `alert=False` and still
carry populated `cluster_details` if its receiver is a known hub.

**Verification done**: no `data/graph/graph_features.csv` exists in this
checkout (batch pipeline never run here), so full end-to-end `uvicorn`
startup wasn't possible. Instead verified via:
- Direct `RealtimeScorer` unit-level smoke test (4 senders → 1 collector,
  confirmed `cluster_details` populates correctly).
- `fastapi.testclient.TestClient` against `main.app` with `main.scorer`
  monkey-patched to a hand-built `RealtimeScorer` (bypasses the
  `graph_features.csv`-required startup hook) — confirmed `/score` and
  `/feed` end-to-end, 5 txns in → `/feed` returns 5, `cluster_details`
  present, `amount_inr`/`account_id_to` attached.
- Created a scratch `.venv` to install `fastapi`/`pandas`/`numpy`/`httpx`
  for the above, then deleted it afterward (not a project convention,
  don't recreate it as a fixture — see README's `venv` naming instead).

**Also synced**: `dashboard/MulePredatorDashboard.jsx` was a stale, older
twin of the `.html` dashboard (missing the two-tier filter, demo mode,
quantum feed panel — it predated several dashboard features, not just this
change). Rewrote it to full parity with `mulepredator_dashboard.html`.
These two files have no build step or test tying them together — sync is
manual and easy to forget. If you change the dashboard, grep both files
before considering the change done.

**Docs written this session**: `PROJECT_OVERVIEW.md` (architecture +
roadmap ideas), `CLAUDE.md` (repo-specific rules for future sessions),
`README.md` rewrite (fixed broken step numbering, documented `/feed` and
`cluster_details`), `api/README.md` endpoint table updated to match.

**Not done / explicitly out of scope this session**: Redis-backed state,
WebSocket push, analyst feedback loop, case management grouping — these
are `PROJECT_OVERVIEW.md` roadmap ideas, not requested yet.
