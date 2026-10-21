import React, { useState, useEffect } from "react";
import useSimulation from "./useSimulation";
import useEmailMetrics from "./useEmailMetrics";
import EmailActivityChart from "./EmailActivityChart";
import AutomationImpact from "./AutomationImpact";
import EmailActivityList from "./EmailActivityList";
import EmailDagPanel from "./EmailDagPanel";
import { noiseMetrics, formatMs, timeSavings } from "./metrics";
// ==========================================
// CONSTANTS & MOCK DATA
// ==========================================
const SCENARIOS = ["Sustained", "Escalating", "Resolving", "Intermittent"];
const INITIAL_EMAILS = Array.from({ length: 12 }, (_, i) => ({
  id: i + 1,
  subject:
    i < 4
      ? "Singtel BSS · DRA Gy TimerExpiry 5012 Count"
      : i < 8
      ? "APIGW average latency threshold reached"
      : "Daily infrastructure health report",
  sender: i < 8 ? "mvne.support@ip-tribe.com" : "reports@ip-tribe.com",
  count: [24673, 131574, 122433, 9060][i % 4],
  alert: i < 8,
  action: i < 4,
  stage:
    i === 0
      ? "executing"
      : i === 1
      ? "failed"
      : i < 4
      ? "completed"
      : "queued",
  scenario: SCENARIOS[i % 4],
}));
const STATE_COLORS = {
  queued: "bg-slate-100 text-slate-500 border-slate-200",
  executing: "bg-blue-50 text-blue-600 border-blue-200",
  completed: "bg-emerald-50 text-emerald-700 border-emerald-200",
  failed: "bg-red-50 text-red-600 border-red-200",
};
const SVG_PATHS = {
  grid: "M3 3h6v6H3z M13 3h6v6h-6z M3 13h6v6H3z M13 13h6v6h-6z",
  mail: "M3 5h18v14H3z M3 5l9 7 9-7",
  flow: "M4 3h6v5H4z M14 14h6v5h-6z M2 14h6v5H2z M7 8v3h10v3 M7 11H5v3",
  bolt: "M13 2L4 13h7l-1 9 10-13h-7z",
  play: "M7 4l14 8-14 8z",
  search: "M16 16l5 5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  check: "M4 12l5 5L20 6",
  close: "M5 5l14 14 M19 5L5 19",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2",
  arrow: "M4 12h16 M15 7l5 5-5 5",
  settings: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 2v3 M12 19v3 M2 12h3 M19 12h3",
  shield: "M12 2l8 4v6c0 5-8 10-8 10S4 17 4 12V6z",
};
// ==========================================
// SUB-COMPONENTS
// ==========================================
function Icon({ name, size = 18 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={SVG_PATHS[name] || SVG_PATHS.grid} />
    </svg>
  );
}
function Badge({ state }) {
  const dotColor =
    state === "executing"
      ? "bg-blue-500 pulse"
      : state === "completed"
      ? "bg-emerald-500"
      : state === "failed"
      ? "bg-red-500"
      : "bg-slate-400";
  const label =
    state === "executing"
      ? "Executing"
      : state === "completed"
      ? "Completed"
      : state === "failed"
      ? "Failed"
      : "Queued";
  return (
    <span
      className={`inline-flex items-center gap-1.5 text-[11px] px-2 py-1 rounded-md border ${STATE_COLORS[state]}`}
    >
      <i className={`dot ${dotColor}`} />
      {label}
    </span>
  );
}
function MetricCard({ label, value, detail, icon }) {
  return (
    <div className="card p-5">
      <div className="flex justify-between text-slate-500 text-xs font-medium">
        {label}
        <Icon name={icon} />
      </div>
      <div className="text-3xl font-semibold tracking-tight mt-4">{value}</div>
      <div className="text-xs text-slate-500 mt-2">{detail}</div>
    </div>
  );
}
function Node({ title, state, sub }) {
  const dotColor =
    state === "executing"
      ? "bg-blue-500 pulse"
      : state === "completed"
      ? "bg-emerald-500"
      : state === "failed"
      ? "bg-red-500"
      : "bg-slate-400";
  return (
    <div className={`border rounded-lg px-3 py-3 min-w-0 ${STATE_COLORS[state]}`}>
      <div className="flex items-center gap-2 text-xs font-semibold">
        <span className={`dot ${dotColor}`} />
        {title}
      </div>
      <div className="text-[10px] mt-1.5 opacity-80">{sub || state}</div>
    </div>
  );
}
// ==========================================
// MAIN APPLICATION COMPONENT
// ==========================================
function App() {
  const [tab, setTab] = useState("Overview");
  const [liveSelectedId, setLiveSelectedId] = useState(null);
  const [selected, setSelected] = useState(INITIAL_EMAILS[0]);
  const [search, setSearch] = useState("");
  const { run, starting, running, ingested, ingestionRate, connection, error: simulationError, startSimulation } = useSimulation();
  const latency = useEmailMetrics(run?.simulation_run_id);
  const noise = noiseMetrics(run);

  const [tick, setTick] = useState(0);
  const [logTab, setLogTab] = useState("Verification");
  const [raw, setRaw] = useState(false);
  const [notice, setNotice] = useState("");
  // Timer for ambient tick update
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);
  const handleOverride = () => setNotice("Human override is not connected to the backend yet.");
  useEffect(() => setLiveSelectedId(null), [run?.simulation_run_id]);
  const handleSimulateBurst = startSimulation;
  return (
    <div className="min-h-screen flex">
      {/* SIDEBAR */}
      <aside className="w-56 bg-[#112632] text-slate-300 shrink-0 hidden lg:flex flex-col fixed inset-y-0">
        <div className="h-20 flex items-center gap-3 px-6 text-white">
          <div className="bg-teal-400/15 text-teal-300 p-2 rounded-lg">
            <Icon name="shield" size={23} />
          </div>
          <div className="font-semibold text-lg tracking-tight">
            tribal eye
            <span className="block text-[9px] uppercase tracking-[.23em] text-slate-400">
              Operations intelligence
            </span>
          </div>
        </div>
        <div className="px-5 py-5">
          <div className="text-[10px] tracking-widest text-slate-500 mb-3">
            WORKSPACE
          </div>
          {[
            ["Overview", "grid"],
            ["Alert inbox", "mail"],
            ["Actionable alerts", "bolt"],
          ].map(([t, icon]) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex items-center gap-3 w-full px-3 py-3 mb-1 rounded-lg text-sm ${
                tab === t
                  ? "bg-teal-400/10 text-teal-300"
                  : "hover:bg-white/5"
              }`}
            >
              <Icon name={icon} />
              {t}
              {t === "Actionable alerts" && (
                <span className="ml-auto text-xs bg-white/10 px-1.5 rounded">
                  {latency.rows.filter(row => row.is_alert === "YES" && row.is_5012 === "YES").length}
                </span>
              )}
            </button>
          ))}
        </div>
        <div className="mt-auto p-5">
          <div className="border border-white/10 rounded-lg p-3 text-xs">
            <div className="flex items-center gap-2 text-teal-300">
              <i className="dot bg-teal-400" /> Demo environment
            </div>
            <p className="text-slate-500 mt-2 leading-5">
              Synthetic telemetry.
              <br />
              No live service actions.
            </p>
          </div>
          <div className="flex items-center gap-3 mt-5">
            <div className="rounded-full bg-slate-600 w-8 h-8 flex items-center justify-center text-xs">
              L1
            </div>
            <div className="text-xs">
              L1 Operations
              <span className="block text-slate-500 mt-1">
                Operator workspace
              </span>
            </div>
          </div>
        </div>
      </aside>
      {/* MAIN CONTENT AREA */}
      <main className="lg:ml-56 flex-1 min-w-0">
        {/* TOP HEADER */}
        <header className="h-16 bg-white border-b px-7 flex items-center justify-between">
          <div className="text-xs text-slate-400">
            Workspace <span className="mx-2">/</span>
            <span className="text-slate-700">{tab}</span>
          </div>
          <div className="flex items-center gap-4 text-xs">
            <span className="text-emerald-600 flex items-center gap-2">
              <i className="dot bg-emerald-500" /> Demo telemetry active
            </span>
            <span className="border-l pl-4 text-slate-500">
              Asia / Singapore
            </span>
          </div>
        </header>
        {/* BODY CONTAINER */}
        <div className="p-5 xl:p-7 max-w-[1600px] mx-auto">
          {/* TITLE & ACTION BAR */}
          <div className="flex flex-wrap justify-between items-center gap-4 mb-6">
            <div>
              <div className="flex gap-2 items-center">
                <h1 className="text-2xl font-semibold tracking-tight">
                  {tab === "Overview" ? "Operations overview" : tab}
                </h1>
                <span className="text-[10px] text-teal-700 bg-teal-50 border border-teal-200 px-2 py-0.5 rounded-full">
                  LIVE DEMO
                </span>
              </div>
              <p className="text-sm text-slate-500 mt-1.5">
                Ingestion, rate, latency and noise metrics are live. Email list is live. DAG is live. Logs are live.
              </p>
            </div>
            <div className="flex gap-3">
              <span className="card px-3 py-2.5 text-xs flex items-center gap-2">
                <Icon name="clock" size={14} /> Today
              </span>
              <button
                disabled={starting || Boolean(run && !["COMPLETED", "FAILED"].includes(run.status))}
                onClick={handleSimulateBurst}
                className="bg-[#137b78] text-white rounded-lg px-4 py-2.5 text-xs font-semibold flex items-center gap-2 disabled:opacity-60"
              >
                <Icon name="play" size={14} />
                {starting ? "Starting…" : running ? "Burst running…" : run && !["COMPLETED", "FAILED"].includes(run.status) ? "Classifying…" : "Simulate Burst"}
              </button>
            </div>
          </div>
          {/* NOTIFICATION BANNER */}
          {(simulationError || notice) && (
            <div
              role="status"
              className="mb-4 bg-teal-50 border border-teal-200 text-teal-800 rounded-lg p-3 text-xs flex justify-between"
            >
              {simulationError || notice}
              <button aria-label="Dismiss" onClick={() => setNotice("")}>
                <Icon name="close" size={14} />
              </button>
            </div>
          )}
          {/* METRIC CARDS */}
          <div className="grid sm:grid-cols-2 xl:grid-cols-4 gap-4 mb-5">
            <MetricCard
              label="Emails ingested"
              value={ingested}
              detail={run ? `${run.total ?? "—"} emails · ${run.status} · SSE ${connection}` : "Click Simulate Burst to start"}
              icon="mail"
            />
            <MetricCard
              label="Ingestion rate"
              value={`${ingestionRate.toFixed(1)} /s`}
              detail="Emails observed over the last 5 seconds"
              icon="bolt"
            />
            <MetricCard
              label="Average latency"
              value={formatMs(latency.averageMs)}
              detail={latency.error || (latency.sampleCount ? `Queued → completed · ${latency.sampleCount} emails · queue wait ${formatMs(latency.queueWaitMs)}` : "Waiting for completed classification timestamps")}
              icon="clock"
            />
            <MetricCard
              label="Noise filtered"
              value={noise.noisePercent === null ? "—" : `${noise.noisePercent.toFixed(1)}%`}
              detail={`${noise.filtered} emails classified as non-alert`}
              icon="shield"
            />
          </div>
          {/* CHARTS SECTION */}
          <AutomationImpact rows={latency.rows} />
          <div className="grid xl:grid-cols-[1.6fr_1fr] gap-5 mb-5">
            {/* INBOUND ACTIVITY CHART */}
            <EmailActivityChart rows={latency.rows} connection={latency.connection} error={latency.error} runId={run?.simulation_run_id} />
            {/* BREAKDOWN DONUT CHART */}
            <section className="card p-5">
              <h2 className="text-sm font-semibold">
                Classification breakdown
              </h2>
              <p className="text-xs text-slate-400 mt-1">
                Every email, accounted for
              </p>
              <div className="flex items-center gap-7 mt-5">
                <div
                  className="w-36 h-36 shrink-0 rounded-full p-4"
                  style={{
                    background:
                      "conic-gradient(#16a39b 0 46%, #3b82f6 46% 50%, #cbd5e1 50% 100%)",
                  }}
                >
                  <div className="w-full h-full rounded-full bg-white flex flex-col items-center justify-center">
                    <strong className="text-3xl">{ingested}</strong>
                    <span className="text-[10px] text-slate-400">
                      total ingested
                    </span>
                  </div>
                </div>
                <div className="flex-1 space-y-3 text-xs">
                  {[
                    ["Noise filtered", "46", "#16a39b"],
                    ["Actionable", "4", "#3b82f6"],
                    ["Other alerts", "50", "#cbd5e1"],
                  ].map(([label, val, color]) => (
                    <div key={label} className="flex items-center gap-2">
                      <i className="dot" style={{ background: color }} />
                      <span className="text-slate-500">{label}</span>
                      <strong className="ml-auto">{val}</strong>
                    </div>
                  ))}
                  <div className="pt-3 border-t text-[10px] text-slate-400">
                    4 automatically routed alerts
                    <br />
                    Resolution confirmation pending
                  </div>
                </div>
              </div>
            </section>
          </div>
          {/* DETAILS & LIST SECTION */}
          <div className="grid xl:grid-cols-[1fr_1.3fr] gap-5">
            <EmailActivityList rows={latency.rows} tab={tab} runId={run?.simulation_run_id} connection={latency.connection} error={latency.error} selectedId={liveSelectedId} onSelect={setLiveSelectedId} />
            <EmailDagPanel row={latency.rows.find(row => row.email_id === liveSelectedId)} selectedId={liveSelectedId} connection={latency.connection} />
          </div>
          {/* FOOTER */}
          <footer className="text-[10px] text-slate-400 mt-5 flex justify-between">
            <span>
              ¹ Current-run elapsed time estimate. Manual timings are assumptions; engineer effort savings are not measured.
            </span>
            <span>Tribal Eye · L1 operations workspace</span>
          </footer>
        </div>
      </main>
    </div>
  );
}
export default App;