# MulePredator 🦅

Real-Time UPI Fraud Intelligence & SOC Console

MulePredator is a high-performance fraud detection pipeline and Security Operations Center (SOC) console. It identifies complex money-laundering operations (like mule networks and smurfing rings) by employing a **multi-signal convergence thesis**.

Rather than overwhelming analysts with single-point anomalies, MulePredator evaluates transactions across three distinct axes **Graph/Network**, **Cyber/Device**, and **Quantum Risk** and only escalates alerts when multiple independent engines agree.

## Run Locally

## Deployment

To deploy this project run

## 🏗️ Project Architecture

- **Data Generation:** Synthesizes realistic banking streams (transactions, auth logs, TLS sessions).
- **Detection Engines:**
  - _Graph Engine:_ Detects structural anomalies like fan-in/fan-out and community clustering.
  - _Cyber Engine:_ Flags account-takeover indicators like impossible travel and device churn.
  - _Quantum Engine:_ A separate risk axis tracking exposure to "Harvest Now, Decrypt Later" threats.
- **Fusion Engine:** The convergence layer that promotes multi-signal threats to High Priority alerts.
- **FastAPI Backend:** Provides real-time transaction scoring in `< 100ms`.
- **React Dashboard:** A live-updating SOC triage console displaying network structures, alert queues, and telemetry.

---

## 🚀 Local Setup & Installation Guide

Follow these steps to generate the data, warm up the engines, and start the live console.

### Prerequisites

- **Python 3.10 or 3.11**
- **Git**
- A modern web browser (Chrome, Edge, Firefox)

### Step 1: Clone & Environment Setup

Open your terminal (Command Prompt/PowerShell or Mac/Linux Terminal) and run:

````bash
# 1. Clone the repository and enter the directory
git clone [https://github.com/Anonymous2512/MulePredator.git]
cd MulePredator

# 2. Create a virtual environment
python -m venv venv

# 3. Activate the virtual environment
# On Windows (Command Prompt): venv\Scripts\activate
# On Windows (PowerShell): .\venv\Scripts\Activate.ps1
# On Mac/Linux: source venv/bin/activate

## 3. Initialize the Pipeline

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

### Step 2: Launch the Live Demo Services

To see the system run in real-time, you need to spin up the API server and the transaction simulator.

#### 🖥️ 2A. Start the FastAPI Server

In your main terminal window, set the data paths and boot the server:

**Windows (PowerShell):**

```powershell
uvicorn main:app --app-dir api --port 8000

```

**Windows (Command Prompt):**

```cmd
uvicorn main:app --app-dir api --port 8000
```

**Mac/Linux:**

```bash
uvicorn main:app --app-dir api --port 8000
```

_(Leave this terminal open. The server is ready when it says `Uvicorn running on http://127.0.0.1:8000`)_

#### 📡 2B. Start the Transaction Replayer

Open a **second terminal window**, navigate to the `MulePredator` folder, activate the virtual environment, and run the simulator:

```bash
# Don't forget to activate the venv again!
python api/replay.py

```

_(This script will continuously fire transactions at your live API)._

---

### Step 5: Open the SOC Dashboard

1. Open your file explorer and navigate to the `MulePredator/dashboard/` folder.
2. Double-click **`mulepredator_dashboard.html`** to open it in your browser.
3. In the top right corner of the dashboard, click **`▶ START FEED`**.

You will now see live transactions streaming into the queue. Clean transactions will flow through silently, while multi-signal anomalies will trigger the convergence logic and pop up as **High Priority** alerts with dynamic network structures!

---

## 🛠️ API Documentation

Once the server is running (Step 2A), you can view the interactive API documentation and test endpoints manually by visiting:
**[http://127.0.0.1:8000/docs](https://www.google.com/search?q=http://127.0.0.1:8000/docs)**

## License

This project was developed for educational and hackathon purposes.
````
