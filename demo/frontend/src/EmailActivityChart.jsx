import React, { useEffect, useMemo, useState } from 'react';
import { buildActivity, ACTIVITY_VIEWS } from './activity';
const timeLabel = value => new Date(value).toLocaleTimeString('en-SG', { timeZone: 'Asia/Singapore', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
export default function EmailActivityChart({ rows, connection, error, runId }) {
  const [now, setNow] = useState(Date.now());
  const [view, setView] = useState("30s");
  const [hovered, setHovered] = useState(null);
  const [frozen, setFrozen] = useState(null);
  const paused = frozen !== null && frozen.runId === runId;
  useEffect(() => { setFrozen(null); setHovered(null); setNow(Date.now()); }, [runId]);
  function togglePause() {
    setHovered(null);
    if (paused) {
      setNow(Date.now());
      setFrozen(null);
    } else {
      setFrozen({
        runId, now,
        rows: rows.map(row => ({ ...row, email: row.email ? { ...row.email } : row.email })),
      });
    }
  }
  useEffect(() => { if (paused) return; const timer = setInterval(() => setNow(Date.now()), 250); return () => clearInterval(timer); }, [paused]);
  const { points, interval } = useMemo(() => buildActivity(paused ? frozen.rows : rows, paused ? frozen.now : now, view), [rows, now, view, frozen, paused]);
  const W = 720, H = 225, left = 42, right = 15, top = 20, bottom = 43;
  const max = Math.max(1, ...points.flatMap(p => [p.inbound, p.processed]));
  const ceiling = Math.ceil(max / 4) * 4;
  const x = i => left + i / (points.length - 1) * (W - left - right);
  const y = n => H - bottom - n / ceiling * (H - top - bottom);
  const line = field => points.map((p,i) => `${x(i)},${y(p[field])}`).join(' ');
  const totals = points.reduce((sum,p) => ({ inbound: sum.inbound + p.inbound, processed: sum.processed + p.processed }), { inbound: 0, processed: 0 });
  return <section className="card p-5">
    <div className="flex flex-wrap justify-between gap-3"><div><h2 className="text-sm font-semibold">Inbound & processed email activity</h2><p className="text-xs text-slate-400 mt-1">Current run · emails per {interval === 250 ? "250ms" : "5s"} · Singapore time</p></div>
      <div className="flex items-center gap-2">
      <button type="button" disabled={!runId} aria-pressed={paused} onClick={togglePause} className="text-xs border rounded px-3 py-1 bg-white disabled:opacity-40">{paused ? 'Resume' : 'Pause'}</button>
      <select aria-label="Activity time window" className="text-xs border rounded px-2 py-1 bg-white" value={view} onChange={event => { setView(event.target.value); setHovered(null); }}>{Object.entries(ACTIVITY_VIEWS).map(([key, config]) => <option key={key} value={key}>{config.label}</option>)}</select></div>
    </div>
    {paused && <p role="status" className="text-xs text-amber-700 mt-3">Chart paused at {timeLabel(frozen.now)}. Processing and live updates continue; resume to return to the current window.</p>}
    <div className="flex flex-wrap gap-4 text-xs mt-4"><span className="text-teal-600">● Inbound · {totals.inbound}</span><span className="text-blue-600">● Processed · {totals.processed}</span></div>
    {error && <p role="alert" className="text-xs text-red-600 mt-2">{error}</p>}
    {!runId ? <p className="text-xs text-slate-400 py-12">Start a simulation to display email activity.</p> : <svg viewBox={`0 0 ${W} ${H}`} className="w-full mt-3" role="img" aria-label="Inbound emails in teal and classification completions in blue">
      {[0,1,2,3,4].map(i => <g key={i}><line x1={left} x2={W-right} y1={y(i*ceiling/4)} y2={y(i*ceiling/4)} stroke="#e8edf1"/><text x={left-8} y={y(i*ceiling/4)+4} textAnchor="end" fontSize="10" fill="#94a3b8">{i*ceiling/4}</text></g>)}
      <polyline points={line('inbound')} fill="none" stroke="#0d9488" strokeWidth="2"/>
      <polyline points={line('processed')} fill="none" stroke="#3b82f6" strokeWidth="2" strokeDasharray="5 3"/>
      {points.map((p,i) => <rect key={p.at} x={x(i) - (W-left-right)/(points.length-1)/2} y={top} width={(W-left-right)/(points.length-1)} height={H-top-bottom} fill="transparent" onMouseEnter={() => setHovered(i)} onMouseLeave={() => setHovered(null)}><title>{timeLabel(p.at)}: inbound {p.inbound}, processed {p.processed}</title></rect>)}
      {hovered !== null && points[hovered] && <g pointerEvents="none"><circle cx={x(hovered)} cy={y(points[hovered].inbound)} r="4" fill="#0d9488"/><circle cx={x(hovered)} cy={y(points[hovered].processed)} r="4" fill="#3b82f6"/></g>}
      {points.map((p,i) => ({...p,i})).filter(p => p.at % (view === "5m" ? 60000 : 5000) === 0).map(p => <text key={p.at} x={x(p.i)} y={H-17} textAnchor={p.i < 3 ? 'start' : p.i > points.length-4 ? 'end' : 'middle'} fontSize="10" fill="#94a3b8">{timeLabel(p.at)}</text>)}
    </svg>}
    <p className="text-[10px] text-slate-400 mt-2">Processed = classification completed or failed. Latest bucket is partial. SSE: {connection}. Verification and modem completion are separate.</p>
  </section>;
}
