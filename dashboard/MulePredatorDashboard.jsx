import React, { useState, useMemo, useEffect, useRef } from "react";

// ============================================================================
// MulePredator — SOC analyst fraud-investigation console
//
// Design intent: an instrument-grade operations console, not a marketing
// dashboard. Restraint is the aesthetic risk: calm dense slate surface, signal
// colors that carry meaning (amber=monitor, red=high-priority, cyan=quantum as
// a SEPARATE axis, green=clear), monospace for all telemetry so numeric columns
// align like a real trading/SOC terminal. Signature element: the three-lane
// convergence meter that shows WHICH engines fired and whether they agreed --
// the product's core thesis ("alert on convergence, not single signals")
// rendered directly in the UI.
//
// Runs standalone on mock data derived from the real pipeline output. To wire
// to the live API, set USE_LIVE_API = true and point API_BASE at the FastAPI
// service; the fetch seams are marked below.
// ============================================================================

const USE_LIVE_API = true;
const API_BASE = "http://localhost:8000";

const C = {
  bg: "#0F1419",
  panel: "#171E27",
  panelHi: "#1E2732",
  line: "#2A3542",
  lineHi: "#3A4757",
  text: "#D6DEE8",
  textDim: "#7C8A9A",
  textFaint: "#4E5A68",
  monitor: "#E0A03C",
  high: "#E8574A",
  investigate: "#E0A03C",
  quantum: "#4ABFD4",
  clear: "#5FB07A",
  graph: "#8B7FE0",
  cyber: "#E8894A",
};

const TIER_META = {
  high_priority: { label: "HIGH PRIORITY", color: C.high, order: 0 },
  investigate: { label: "INVESTIGATE", color: C.investigate, order: 1 },
  monitor: { label: "MONITOR", color: C.monitor, order: 2 },
};

// ---- mock data derived from actual pipeline output shapes ----
const MOCK_ALERTS = [
  { txn_id: "fc446787-0000-40", account: "0000150c", tier: "high_priority", fraud_score: 0.83, graph: 0.83, cyber: 0.4, quantum: 0.0, signals: 2, amount: 1673, reason: "CONVERGED (2 independent signals) — cyber: elevated cyber score (0.40); graph: small tightly-clustered community of 42 accounts", ts: "07-01 14:22:07" },
  { txn_id: "a81c0043-0000-40", account: "00003af1", tier: "high_priority", fraud_score: 1.0, graph: 1.0, cyber: 1.0, quantum: 0.0, signals: 2, amount: 48200, reason: "CONVERGED (2 independent signals) — cyber: failed login burst; new device; device/IP churn; graph: extreme fan-in (possible collector/hub)", ts: "07-01 14:19:51" },
  { txn_id: "5d9f1120-0000-40", account: "00005473", tier: "high_priority", fraud_score: 0.92, graph: 0.92, cyber: 0.6, quantum: 0.85, signals: 2, amount: 92000, reason: "CONVERGED (2 independent signals) — cyber: device/IP churn; graph: small tightly-clustered community of 31 accounts | SEPARATE quantum exposure: TLSv1.0, 1024-bit key, no forward secrecy, 512.0MB transferred", ts: "07-01 14:15:33" },
  { txn_id: "3e7a8890-0000-40", account: "000088f8", tier: "investigate", fraud_score: 0.65, graph: 0.65, cyber: 0.4, quantum: 0.0, signals: 2, amount: 8400, reason: "CONVERGED (2 independent signals) — cyber: elevated cyber score (0.40); graph: high betweenness (pivot account within its cluster)", ts: "07-01 14:11:02" },
  { txn_id: "9f2b4451-0000-40", account: "00004472", tier: "investigate", fraud_score: 0.58, graph: 0.58, cyber: 0.35, quantum: 0.0, signals: 2, amount: 15600, reason: "CONVERGED (2 independent signals) — cyber: elevated cyber score (0.35); graph: small tightly-clustered community of 58 accounts", ts: "07-01 14:08:44" },
  { txn_id: "c04d7783-0000-40", account: "0000926f", tier: "monitor", fraud_score: 0.79, graph: 0.79, cyber: 0.15, quantum: 0.0, signals: 1, amount: 3200, reason: "single signal only — graph: small tightly-clustered community of 38 accounts", ts: "07-01 14:05:19" },
  { txn_id: "b12e9964-0000-40", account: "00002ff0", tier: "monitor", fraud_score: 0.6, graph: 0.0, cyber: 0.6, quantum: 0.0, signals: 1, amount: 21000, reason: "single signal only — cyber: new device; impossible travel velocity", ts: "07-01 14:02:55" },
  { txn_id: "77a3c105-0000-40", account: "00001a2b", tier: "monitor", fraud_score: 0.55, graph: 0.55, cyber: 0.2, quantum: 0.7, signals: 1, amount: 67000, reason: "single signal only — graph: extreme fan-in (possible collector/hub) | SEPARATE quantum exposure: TLSv1.2, 2048-bit key, no forward secrecy, 340.0MB transferred", ts: "07-01 13:58:41" },
];

// a real ring: collector + spokes (smurfing-style star) for the network view
const MOCK_RING = {
  nodes: [
    { id: "collector", label: "00003af1", role: "collector", risk: 1.0 },
    ...Array.from({ length: 11 }, (_, i) => ({ id: `s${i}`, label: `spoke_${i}`, role: "spoke", risk: 0.3 + Math.random() * 0.2 })),
    { id: "cashout", label: "0000e2d1", role: "cashout", risk: 0.9 },
  ],
  edges: [
    ...Array.from({ length: 11 }, (_, i) => ({ from: `s${i}`, to: "collector", amt: 8000 + Math.floor(Math.random() * 40000) })),
    { from: "collector", to: "cashout", amt: 420000 },
  ],
};

const MOCK_QUANTUM = [
  { account: "00005473", tls: "TLSv1.0", key: 1024, fs: false, vol: 512, score: 0.85 },
  { account: "0000926f", tls: "TLSv1.0", key: 1024, fs: false, vol: 388, score: 0.82 },
  { account: "00001a2b", tls: "TLSv1.2", key: 2048, fs: false, vol: 340, score: 0.7 },
  { account: "00007c40", tls: "TLSv1.2", key: 2048, fs: false, vol: 210, score: 0.63 },
];

const STATS = { scored: 2017255, alerts: 3046, quantum: 199201, p99: 0.05, tput: 21000 };

// ---- convergence meter: the signature element ----
function ConvergenceMeter({ graph, cyber, quantum, signals, compact }) {
  const lane = (label, val, color, fired) => (
    <div style={{ display: "flex", alignItems: "center", gap: 6, opacity: fired ? 1 : 0.32 }}>
      <span style={{ width: compact ? 30 : 42, fontSize: 9, letterSpacing: 0.5, color: C.textDim, fontFamily: "'JetBrains Mono', monospace" }}>{label}</span>
      <div style={{ flex: 1, height: compact ? 4 : 6, background: C.bg, borderRadius: 2, overflow: "hidden", position: "relative" }}>
        <div style={{ position: "absolute", inset: 0, width: `${val * 100}%`, background: color, borderRadius: 2, transition: "width .4s ease" }} />
      </div>
      <span style={{ width: 26, textAlign: "right", fontSize: 10, color: fired ? color : C.textFaint, fontFamily: "'JetBrains Mono', monospace" }}>{val.toFixed(2)}</span>
    </div>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: compact ? 3 : 5, width: "100%" }}>
      {lane("GRAPH", graph, C.graph, graph >= 0.3)}
      {lane("CYBER", cyber, C.cyber, cyber >= 0.3)}
      {!compact && lane("QUANT", quantum, C.quantum, quantum >= 0.6)}
      {!compact && (
        <div style={{ marginTop: 2, fontSize: 9, letterSpacing: 1, fontFamily: "'JetBrains Mono', monospace", color: signals >= 2 ? C.high : C.textDim }}>
          {signals >= 2 ? "◆ CONVERGED — 2 INDEPENDENT FRAUD SIGNALS" : "○ SINGLE SIGNAL — BELOW ALERT THRESHOLD"}
        </div>
      )}
    </div>
  );
}

// ---- network graph (SVG force-ish static layout for a ring) ----
function NetworkView({ ring }) {
  const w = 440, h = 340, cx = w / 2, cy = h / 2;
  const pos = useMemo(() => {
    const p = {};
    ring.nodes.forEach((n, i) => {
      if (n.role === "collector") p[n.id] = { x: cx, y: cy };
      else if (n.role === "cashout") p[n.id] = { x: cx, y: cy - 130 };
      else {
        const spokes = ring.nodes.filter((x) => x.role === "spoke");
        const idx = spokes.indexOf(n);
        const ang = (idx / spokes.length) * Math.PI * 2;
        p[n.id] = { x: cx + Math.cos(ang) * 135, y: cy + Math.sin(ang) * 115 };
      }
    });
    return p;
  }, [ring]);

  const color = (role, risk) => role === "collector" ? C.high : role === "cashout" ? C.monitor : `rgba(139,127,224,${0.4 + risk * 0.6})`;

  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: "auto" }}>
      {ring.edges.map((e, i) => {
        const a = pos[e.from], b = pos[e.to];
        const big = e.amt > 100000;
        return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={big ? C.monitor : C.lineHi} strokeWidth={big ? 2 : 1} opacity={big ? 0.8 : 0.5} />;
      })}
      {ring.nodes.map((n) => {
        const p = pos[n.id];
        const r = n.role === "collector" ? 16 : n.role === "cashout" ? 13 : 8;
        return (
          <g key={n.id}>
            <circle cx={p.x} cy={p.y} r={r} fill={color(n.role, n.risk)} stroke={C.bg} strokeWidth={2} />
            {(n.role === "collector" || n.role === "cashout") && (
              <text x={p.x} y={p.y + r + 12} textAnchor="middle" fontSize={9} fill={C.textDim} fontFamily="'JetBrains Mono', monospace">{n.label}</text>
            )}
          </g>
        );
      })}
      <text x={cx} y={cy + 4} textAnchor="middle" fontSize={8} fill={C.bg} fontFamily="'JetBrains Mono', monospace" fontWeight="700">HUB</text>
    </svg>
  );
}

function StatCard({ label, value, sub, color }) {
  return (
    <div style={{ flex: 1, padding: "12px 14px", background: C.panel, border: `1px solid ${C.line}`, borderRadius: 6 }}>
      <div style={{ fontSize: 9, letterSpacing: 1, color: C.textDim, fontFamily: "'JetBrains Mono', monospace", marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600, color: color || C.text, fontFamily: "'JetBrains Mono', monospace", lineHeight: 1 }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: C.textFaint, marginTop: 4, fontFamily: "'JetBrains Mono', monospace" }}>{sub}</div>}
    </div>
  );
}

export default function MulePredatorDashboard() {
  const [alerts, setAlerts] = useState(MOCK_ALERTS);
  const [selected, setSelected] = useState(MOCK_ALERTS[1]);
  const [filter, setFilter] = useState("all");
  const [live, setLive] = useState(false);
  const liveRef = useRef(null);

  // live feed simulation: periodically prepend a new alert (or fetch from API)
  useEffect(() => {
    if (!live) { if (liveRef.current) clearInterval(liveRef.current); return; }
    liveRef.current = setInterval(async () => {
      if (USE_LIVE_API) {
        // LIVE SEAM: fetch newest alerts from the API
        try {
          const r = await fetch(`${API_BASE}/alerts?limit=20`);
          const j = await r.json();
          if (j.alerts) setAlerts(j.alerts.map(mapApiAlert));
        } catch (e) { /* API not reachable; stay on mock */ }
      } else {
        const base = MOCK_ALERTS[Math.floor(Math.random() * MOCK_ALERTS.length)];
        const now = new Date();
        const stamp = `07-01 ${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
        setAlerts((prev) => [{ ...base, txn_id: Math.random().toString(16).slice(2, 18), ts: stamp }, ...prev].slice(0, 40));
      }
    }, 2200);
    return () => clearInterval(liveRef.current);
  }, [live]);

  const filtered = useMemo(() => {
    const a = filter === "all" ? alerts : alerts.filter((x) => x.tier === filter);
    return [...a].sort((x, y) => TIER_META[x.tier].order - TIER_META[y.tier].order);
  }, [alerts, filter]);

  return (
    <div style={{ minHeight: "100vh", background: C.bg, color: C.text, fontFamily: "'Inter', system-ui, sans-serif" }}>
      <style>{`@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600;700&display=swap');
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-thumb { background: ${C.line}; border-radius: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
      `}</style>

      {/* header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 22px", borderBottom: `1px solid ${C.line}`, background: C.panel }}>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <div style={{ width: 28, height: 28, borderRadius: 6, background: `linear-gradient(135deg, ${C.high}, ${C.graph})`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, fontWeight: 700 }}>M</div>
          <div>
            <div style={{ fontSize: 15, fontWeight: 600, letterSpacing: 0.3 }}>MulePredator</div>
            <div style={{ fontSize: 10, color: C.textDim, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 0.5 }}>UPI FRAUD INTELLIGENCE · SOC CONSOLE</div>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 11, fontFamily: "'JetBrains Mono', monospace", color: C.textDim }}>
            <span style={{ width: 7, height: 7, borderRadius: "50%", background: live ? C.clear : C.textFaint, boxShadow: live ? `0 0 8px ${C.clear}` : "none" }} />
            {live ? "LIVE FEED" : "PAUSED"}
          </div>
          <button onClick={() => setLive((v) => !v)} style={{ padding: "7px 14px", fontSize: 11, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 0.5, background: live ? C.panelHi : C.high, color: live ? C.text : "#fff", border: `1px solid ${live ? C.line : C.high}`, borderRadius: 5, cursor: "pointer", fontWeight: 600 }}>
            {live ? "◼ STOP" : "▶ START FEED"}
          </button>
        </div>
      </div>

      {/* stat strip */}
      <div style={{ display: "flex", gap: 12, padding: "16px 22px" }}>
        <StatCard label="TRANSACTIONS SCORED" value={STATS.scored.toLocaleString()} sub="synthetic UPI stream" />
        <StatCard label="CONVERGED ALERTS" value={STATS.alerts.toLocaleString()} sub="2+ signals agree" color={C.high} />
        <StatCard label="QUANTUM EXPOSURE" value={STATS.quantum.toLocaleString()} sub="separate risk axis" color={C.quantum} />
        <StatCard label="P99 LATENCY" value={`${STATS.p99} ms`} sub="target < 100 ms" color={C.clear} />
        <StatCard label="THROUGHPUT" value={`${(STATS.tput / 1000).toFixed(0)}k/s`} sub="single process" color={C.clear} />
      </div>

      {/* main grid */}
      <div style={{ display: "grid", gridTemplateColumns: "360px 1fr 300px", gap: 12, padding: "0 22px 22px", alignItems: "start" }}>

        {/* LEFT — alert queue */}
        <div style={{ background: C.panel, border: `1px solid ${C.line}`, borderRadius: 8, overflow: "hidden" }}>
          <div style={{ padding: "12px 14px", borderBottom: `1px solid ${C.line}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 11, letterSpacing: 1, fontFamily: "'JetBrains Mono', monospace", color: C.textDim }}>ALERT QUEUE</span>
            <div style={{ display: "flex", gap: 4 }}>
              {["all", "high_priority", "investigate", "monitor"].map((f) => (
                <button key={f} onClick={() => setFilter(f)} style={{ padding: "3px 7px", fontSize: 9, fontFamily: "'JetBrains Mono', monospace", background: filter === f ? C.panelHi : "transparent", color: filter === f ? C.text : C.textFaint, border: `1px solid ${filter === f ? C.lineHi : "transparent"}`, borderRadius: 4, cursor: "pointer", textTransform: "uppercase" }}>
                  {f === "all" ? "ALL" : f === "high_priority" ? "HIGH" : f === "investigate" ? "INV" : "MON"}
                </button>
              ))}
            </div>
          </div>
          <div style={{ maxHeight: 560, overflowY: "auto" }}>
            {filtered.map((a) => {
              const meta = TIER_META[a.tier];
              const sel = selected?.txn_id === a.txn_id;
              return (
                <div key={a.txn_id} onClick={() => setSelected(a)} style={{ padding: "11px 14px", borderBottom: `1px solid ${C.line}`, borderLeft: `3px solid ${meta.color}`, background: sel ? C.panelHi : "transparent", cursor: "pointer" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 7 }}>
                    <span style={{ fontSize: 9, fontWeight: 700, letterSpacing: 0.8, color: meta.color, fontFamily: "'JetBrains Mono', monospace" }}>{meta.label}</span>
                    <span style={{ fontSize: 9, color: C.textFaint, fontFamily: "'JetBrains Mono', monospace" }}>{a.ts}</span>
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8, fontFamily: "'JetBrains Mono', monospace" }}>
                    <span style={{ fontSize: 11, color: C.text }}>acct ···{a.account}</span>
                    <span style={{ fontSize: 11, color: C.textDim }}>₹{a.amount.toLocaleString()}</span>
                  </div>
                  <ConvergenceMeter graph={a.graph} cyber={a.cyber} quantum={a.quantum} signals={a.signals} compact />
                </div>
              );
            })}
          </div>
        </div>

        {/* CENTER — case detail + network */}
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {selected && (
            <div style={{ background: C.panel, border: `1px solid ${C.line}`, borderRadius: 8, padding: 18 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
                <div>
                  <div style={{ fontSize: 10, letterSpacing: 1, color: C.textDim, fontFamily: "'JetBrains Mono', monospace", marginBottom: 4 }}>CASE DETAIL</div>
                  <div style={{ fontSize: 18, fontWeight: 600, fontFamily: "'JetBrains Mono', monospace" }}>txn ···{selected.txn_id.slice(-10)}</div>
                </div>
                <div style={{ textAlign: "right" }}>
                  <div style={{ fontSize: 10, color: C.textDim, fontFamily: "'JetBrains Mono', monospace", marginBottom: 4 }}>FRAUD SCORE</div>
                  <div style={{ fontSize: 28, fontWeight: 700, color: TIER_META[selected.tier].color, fontFamily: "'JetBrains Mono', monospace", lineHeight: 1 }}>{selected.fraud_score.toFixed(2)}</div>
                </div>
              </div>

              <div style={{ padding: 14, background: C.bg, borderRadius: 6, marginBottom: 16 }}>
                <ConvergenceMeter graph={selected.graph} cyber={selected.cyber} quantum={selected.quantum} signals={selected.signals} />
              </div>

              <div style={{ fontSize: 10, letterSpacing: 1, color: C.textDim, fontFamily: "'JetBrains Mono', monospace", marginBottom: 8 }}>WHY THIS FIRED</div>
              <div style={{ fontSize: 13, lineHeight: 1.6, color: C.text, background: C.bg, padding: 14, borderRadius: 6, borderLeft: `3px solid ${TIER_META[selected.tier].color}` }}>
                {selected.reason}
              </div>

              <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
                <button style={{ flex: 1, padding: "10px", fontSize: 11, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 0.5, background: C.high, color: "#fff", border: "none", borderRadius: 5, cursor: "pointer", fontWeight: 600 }}>BLOCK & ESCALATE</button>
                <button style={{ flex: 1, padding: "10px", fontSize: 11, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 0.5, background: C.panelHi, color: C.text, border: `1px solid ${C.lineHi}`, borderRadius: 5, cursor: "pointer" }}>STEP-UP AUTH</button>
                <button style={{ flex: 1, padding: "10px", fontSize: 11, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 0.5, background: "transparent", color: C.textDim, border: `1px solid ${C.line}`, borderRadius: 5, cursor: "pointer" }}>MARK BENIGN</button>
              </div>
            </div>
          )}

          <div style={{ background: C.panel, border: `1px solid ${C.line}`, borderRadius: 8, padding: 18 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <span style={{ fontSize: 10, letterSpacing: 1, color: C.textDim, fontFamily: "'JetBrains Mono', monospace" }}>NETWORK · MULE CLUSTER</span>
              <span style={{ fontSize: 10, color: C.textFaint, fontFamily: "'JetBrains Mono', monospace" }}>13 accounts · 1 collector · 1 cash-out</span>
            </div>
            <div style={{ fontSize: 11, color: C.textDim, marginBottom: 8 }}>Star pattern: 11 disposable senders funnel into one collector, then a single large cash-out. The collector is the actionable node.</div>
            <NetworkView ring={MOCK_RING} />
            <div style={{ display: "flex", gap: 16, justifyContent: "center", marginTop: 8, fontSize: 10, fontFamily: "'JetBrains Mono', monospace", color: C.textDim }}>
              <span><span style={{ color: C.high }}>●</span> collector</span>
              <span><span style={{ color: C.monitor }}>●</span> cash-out</span>
              <span><span style={{ color: C.graph }}>●</span> mule sender</span>
              <span><span style={{ color: C.monitor }}>—</span> large transfer</span>
            </div>
          </div>
        </div>

        {/* RIGHT — quantum exposure panel (separate axis) */}
        <div style={{ background: C.panel, border: `1px solid ${C.line}`, borderRadius: 8, overflow: "hidden" }}>
          <div style={{ padding: "12px 14px", borderBottom: `1px solid ${C.line}`, borderTop: `2px solid ${C.quantum}` }}>
            <div style={{ fontSize: 11, letterSpacing: 1, fontFamily: "'JetBrains Mono', monospace", color: C.quantum }}>QUANTUM EXPOSURE</div>
            <div style={{ fontSize: 10, color: C.textDim, marginTop: 4, lineHeight: 1.5 }}>Harvest-now-decrypt-later risk. A <em>separate</em> axis from fraud — flags quantum-vulnerable crypto on high-value flows, not an attack in progress.</div>
          </div>
          <div style={{ padding: "6px 0" }}>
            {MOCK_QUANTUM.map((q) => (
              <div key={q.account} style={{ padding: "11px 14px", borderBottom: `1px solid ${C.line}` }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                  <span style={{ fontSize: 11, fontFamily: "'JetBrains Mono', monospace", color: C.text }}>···{q.account}</span>
                  <span style={{ fontSize: 12, fontWeight: 700, color: C.quantum, fontFamily: "'JetBrains Mono', monospace" }}>{q.score.toFixed(2)}</span>
                </div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 9, padding: "2px 6px", background: C.bg, borderRadius: 3, color: q.tls === "TLSv1.0" ? C.high : C.monitor, fontFamily: "'JetBrains Mono', monospace" }}>{q.tls}</span>
                  <span style={{ fontSize: 9, padding: "2px 6px", background: C.bg, borderRadius: 3, color: q.key === 1024 ? C.high : C.textDim, fontFamily: "'JetBrains Mono', monospace" }}>{q.key}-bit</span>
                  {!q.fs && <span style={{ fontSize: 9, padding: "2px 6px", background: C.bg, borderRadius: 3, color: C.monitor, fontFamily: "'JetBrains Mono', monospace" }}>no-FS</span>}
                  <span style={{ fontSize: 9, padding: "2px 6px", background: C.bg, borderRadius: 3, color: C.textDim, fontFamily: "'JetBrains Mono', monospace" }}>{q.vol}MB</span>
                </div>
              </div>
            ))}
          </div>
          <div style={{ padding: "12px 14px", fontSize: 10, color: C.textFaint, lineHeight: 1.5, fontFamily: "'JetBrains Mono', monospace" }}>
            Aligns with NIST PQC migration guidance. Flags where sensitive value moves over crypto a future quantum computer could break.
          </div>
        </div>
      </div>
    </div>
  );
}

// LIVE SEAM: shape an API /alerts item into the dashboard's alert shape
function mapApiAlert(a) {
  return {
    txn_id: String(a.txn_id).slice(0, 16),
    account: String(a.account_id || a.account_id_from || "").slice(-8),
    tier: a.alert_tier,
    fraud_score: a.fraud_score,
    graph: a.graph_risk_score,
    cyber: a.cyber_risk_score,
    quantum: a.quantum_exposure_score,
    signals: a.n_fraud_signals,
    amount: Math.round(a.amount_inr || 0),
    reason: a.reason,
    ts: new Date().toISOString().slice(5, 19).replace("T", " "),
  };
}
