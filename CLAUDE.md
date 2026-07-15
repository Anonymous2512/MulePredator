# MulePredator — CLAUDE.md

Real-time UPI mule/fraud detection pipeline + SOC dashboard. Core thesis:
alert on **convergence** (2+ independent engines agreeing), not single
weak signals. Full architecture and roadmap ideas: [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md).

## Architecture split (read this before touching scoring logic)

- **Offline/batch** (`data-generator/`, `engines/*.py`): synthesizes data,
  computes Louvain communities + fan-in hub scores, writes
  `data/graph/graph_features.csv`. Slow, run periodically.
- **Online/real-time** (`api/realtime_scorer.py` + `api/main.py`): scores a
  single transaction in <1ms using the *cached* graph score plus
  *live-computed* cyber/quantum signals. Thresholds and fusion logic here
  are deliberately duplicated from the batch engines to keep live decisions
  matching offline-validated behavior — **if you change a threshold or the
  fusion rule in one place, change it in both**, or you'll silently
  reintroduce the ~0.25% parity gap documented in `realtime_scorer.py`.

## Key files

- `api/realtime_scorer.py` — scoring logic, `AccountState` rolling window,
  `cluster_details` construction (identifies fan-in collectors from live
  inbound state, not from static mock data).
- `api/main.py` — FastAPI app. `/score`, `/feed` (rolling window of ALL
  scored txns, clean+flagged), `/alerts` (flagged only), `/account/{id}`,
  `/stats`, `/health`.
- `engines/graph_engine.py` — heavily commented with *why* each threshold
  was chosen (empirically swept, not guessed). Read the module docstring
  and inline NOTEs before changing `LOUVAIN_RESOLUTION`, `SEED_PERCENTILE`,
  or the hub/community bucket tables — they're calibrated against measured
  fraud lift, not arbitrary.
- `dashboard/mulepredator_dashboard.html` — the dashboard actually launched
  per the README (double-click to open, no build step). Self-contained:
  React/Babel from CDN, all styles inline.
- `dashboard/MulePredatorDashboard.jsx` — bundler-friendly twin of the same
  UI for embedding in a larger React app. **These two files must be kept in
  sync by hand** — there's no build step or test that enforces it. When you
  change one, change the other in the same commit.
- `config.yaml` — controls every knob of the synthetic data generator
  (fraud ring counts/sizes, prevalence targets, noise rates). The
  `validation` block documents the expected fraud prevalence range and
  *why* label reconciliation lowered it from ~2.4% to ~1.8% — read that
  comment before assuming prevalence drift is a bug.

## Running things

No `.venv` exists by default — create one first: `python -m venv venv`
(the README's setup steps use `venv`, not `.venv`). Install with
`pip install -r requirements.txt`.

The API **will not start** without `data/graph/graph_features.csv` — run
the batch pipeline first (see README Step 2). For quick backend-only
smoke tests that don't need real data, construct a `RealtimeScorer`
directly with a small hand-built `graph_scores`/`graph_reasons` dict rather
than running the full 2M-transaction generator — see the git history around
the `/feed` and `cluster_details` additions for an example pattern using
`fastapi.testclient.TestClient`.

## Gotchas

- `cluster_details` is keyed off the transaction's **receiver**
  (`account_id_to`), not the sender — collectors accumulate fan-in as
  receivers. Don't key it off `account_id_from`.
- The dashboard's "Suspicious" vs "Flagged" primary filter is `n_fraud_signals == 1`
  vs `>= 2`, not a tier lookup. Tier (`high_priority`/`investigate`/`monitor`/`none`)
  is a separate secondary filter — don't conflate the two axes.
- `/feed` stores *every* scored transaction (maxlen 100); `/alerts` stores
  only flagged ones (maxlen 500). Picking the wrong one changes what the
  dashboard shows, not just how much.
- Rolling per-account state in `realtime_scorer.py` is in-process memory —
  restarting the API loses it. Not backed by Redis (yet); see
  `PROJECT_OVERVIEW.md` for why that's flagged as the main production gap.

## Workflow notes

- No test suite exists in this repo yet. When changing scoring logic,
  smoke-test with a small hand-built scenario via `TestClient` (see above)
  before claiming a fix works — don't rely on reading the code alone.
- `to-fix.md` at the repo root is the user's running scratch list of
  requested changes; check it for open items before starting new work.
