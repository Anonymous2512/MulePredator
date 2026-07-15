# MulePredator 🦅

Real-Time UPI Fraud Intelligence & SOC Console

MulePredator is a high-performance fraud detection pipeline and Security Operations Center (SOC) console. It identifies complex money-laundering operations (like mule networks and smurfing rings) by employing a **multi-signal convergence thesis**.

Rather than overwhelming analysts with single-point anomalies, MulePredator evaluates transactions across three distinct axes — **Graph/Network**, **Cyber/Device**, and **Quantum Risk** — and only escalates alerts when multiple independent engines agree.

## 🏗️ Project Architecture

The system splits into an **offline/batch** half and an **online/real-time**
half: slow graph intelligence is precomputed periodically and cached, while
per-transaction scoring stays fast by combining that cache with signals
computed live.

- **Data Generation** (`data-generator/`): Synthesizes realistic banking
  streams (transactions, auth logs, TLS sessions) from `config.yaml`.
- **Detection Engines** (`engines/`, offline):
  - _Graph Engine:_ Detects structural anomalies like fan-in/fan-out and
    community clustering; writes per-account `graph_risk_score` +
    `graph_reason` to `data/graph/graph_features.csv`.
  - _Cyber Engine:_ Flags account-takeover indicators like impossible
    travel and device churn.
  - _Quantum Engine:_ A separate risk axis tracking exposure to "Harvest
    Now, Decrypt Later" threats.
- **Fusion Engine:** The convergence layer that promotes multi-signal
  threats to High Priority alerts.
- **FastAPI Backend** (`api/`, online): `realtime_scorer.py` scores a
  transaction in `< 100ms` by combining the cached graph score with
  live-computed cyber/quantum signals and per-account rolling state
  (in-process, not yet Redis-backed). `main.py` exposes it over HTTP:
  `/score` (single transaction, includes `cluster_details` when the
  receiving account looks like a fan-in collector), `/feed` (rolling
  window of every scored transaction), `/alerts` (flagged only),
  `/account/{id}`, `/stats`, `/health`.
- **React Dashboard** (`dashboard/`): A live-updating SOC triage console —
  two-tier transaction filter (scope × tier), a network view rendering the
  real mule cluster from `cluster_details`, and a separate quantum-exposure
  panel. Shipped as a self-contained `mulepredator_dashboard.html` (the one
  the setup steps below launch) plus a bundler-friendly `.jsx` twin kept in
  sync by hand.

See [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) for a deeper walkthrough of
each piece and ideas for what to build next, and [`CLAUDE.md`](CLAUDE.md)
for repo-specific gotchas (offline/online threshold duplication, the
html/jsx sync requirement, etc).

---

## 🚀 Local Setup & Installation Guide

Follow these steps to generate the data, warm up the engines, and start the live console.

### Prerequisites

- **Python 3.10 or 3.11**
- **Git**
- A modern web browser (Chrome, Edge, Firefox)

### Step 1: Clone & Environment Setup

```bash
# 1. Clone the repository and enter the directory
git clone https://github.com/Anonymous2512/MulePredator.git
cd MulePredator

# 2. Create a virtual environment
python -m venv venv

# 3. Activate the virtual environment
# On Windows (PowerShell): .\venv\Scripts\Activate.ps1
# On Windows (Command Prompt): venv\Scripts\activate
# On Mac/Linux: source venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt
```

### Step 2: Initialize the Pipeline

This single command:

- installs dependencies
- generates synthetic banking data
- creates graph features
- builds ML feature tables
- initializes all detection engines

```bash
python setup_pipeline.py
```

Wait until the script prints:

```
SUCCESS
```

---

### Step 3: Launch the Live Demo Services

#### 🖥️ 3A. Start the FastAPI Server

```powershell
uvicorn main:app --app-dir api --port 8000
```

_(Leave this terminal open. The server is ready when it says `Uvicorn running on http://127.0.0.1:8000`)_

#### 📡 3B. Start the Transaction Replayer

Open a **second terminal window**, activate the virtual environment again, and run:

```bash
python api/replay.py
```

_(This script continuously fires transactions at your live API.)_

### Step 4: Open the SOC Dashboard

1. Open your file explorer and navigate to the `MulePredator/dashboard/` folder.
2. Double-click **`mulepredator_dashboard.html`** to open it in your browser.
3. In the top right corner of the dashboard, click **`▶ START FEED`**.

You will now see live transactions streaming into the queue. Clean
transactions flow through silently; single-signal transactions land in
**Suspicious**; two-or-more-signal convergence lands in **Flagged** and, at
high fraud scores, pops up as a **High Priority** alert with the real mule
cluster (collector + senders) rendered in the Network panel.

> `dashboard/MulePredatorDashboard.jsx` is a bundler-friendly twin of the
> same UI (same components, same `/feed` live seam) for embedding in a
> larger React app. `mulepredator_dashboard.html` is the one the steps above
> actually launch. Keep both in sync when changing the dashboard.

---

## 🛠️ API Documentation

Once the server is running (Step 3A), view the interactive API docs at
**http://127.0.0.1:8000/docs**.

| Method | Path                                  | Purpose                                                                                                       |
| ------ | ------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| POST   | `/score`                              | Score a single transaction; returns decision, tier, per-engine scores, `cluster_details`, reason, latency     |
| GET    | `/feed?limit=100`                     | Rolling window of the last 100 scored transactions (clean + flagged), newest first — what the dashboard polls |
| GET    | `/alerts?limit=50&tier=high_priority` | Recent _flagged_ alerts only                                                                                  |
| GET    | `/account/{account_id}`               | Cached graph intelligence + live rolling state for one account                                                |
| GET    | `/stats`                              | Running counters: scored, alerts, avg latency                                                                 |
| GET    | `/health`                             | Liveness check                                                                                                |

`cluster_details` is populated on a transaction whenever its **receiving**
account looks like a fan-in collector (graph engine flagged it, and it has
3+ distinct senders in its trailing window). It contains the real
`collector_account_id` and `sender_account_ids`/`spokes` (with amounts) —
this is what the dashboard's Network panel renders instead of a mock ring.

See [`api/README.md`](api/README.md) for the full offline/online
architecture split and measured performance numbers.

## License

This project was developed for educational and hackathon purposes.
