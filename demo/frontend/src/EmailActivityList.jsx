import React, { useState } from 'react';
export function statusStyle(status = '') {
  if (/^EXECUTING/.test(status)) return 'bg-blue-50 text-blue-700 border-blue-200';
  if (/^FAILED/.test(status)) return 'bg-red-50 text-red-700 border-red-200';
  if (/^COMPLETE(?:D)?_/.test(status)) return 'bg-emerald-50 text-emerald-700 border-emerald-200';
  return 'bg-slate-100 text-slate-600 border-slate-200';
}
function StatusTag({ domain, status }) {
  if (!status) return null;
  return <span title={status} className={`border rounded px-2 py-1 text-[10px] break-all ${statusStyle(status)}`}><span className={status.startsWith('EXECUTING') ? 'pulse' : ''}>●</span> {domain}: {status}</span>;
}
function Prediction({ label, value }) {
  const color = value === 'YES' ? 'bg-teal-50 text-teal-700 border-teal-200' : value === 'NO' ? 'bg-slate-100 text-slate-600 border-slate-200' : value === 'UNKNOWN' ? 'bg-amber-50 text-amber-700 border-amber-200' : 'bg-white text-slate-400 border-slate-200';
  return <span className={`border rounded px-2 py-1 text-[10px] ${color}`}>{label}: {value || 'PENDING'}</span>;
}
export function receivedLabel(value) {
  if (!value) return 'Received time unavailable';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return 'Received time unavailable';
  return date.toLocaleString('en-SG', { timeZone: 'Asia/Singapore', year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}
export default function EmailActivityList({ rows, tab, runId, connection, error, selectedId, onSelect }) {
  const [search, setSearch] = useState('');
  const [alertFilter, setAlertFilter] = useState('ALL');
  const [typeFilter, setTypeFilter] = useState('ALL');
  const matchesPrediction = (value, filter) => filter === 'ALL' || (filter === 'PENDING' ? value == null || value === '' : value === filter);
  const unique = [...new Map(rows.map(row => [row.email_id, row])).values()];
  const visible = unique.filter(row => {
    const email = row.email || {};
    const matchesTab = tab === 'Actionable alerts' ? row.is_alert === 'YES' && row.is_5012 === 'YES' : tab === 'Alert inbox' ? row.is_alert === 'YES' : true;
    return matchesTab && matchesPrediction(row.is_alert, alertFilter) && matchesPrediction(row.is_5012, typeFilter) && `${email.subject || ''} ${email.sender || ''}`.toLowerCase().includes(search.toLowerCase());
  }).sort((a,b) => (Date.parse(b.queued_at) || 0) - (Date.parse(a.queued_at) || 0) || a.email_id.localeCompare(b.email_id));
  return <section className="card overflow-hidden">
    <div className="p-5 flex justify-between items-start gap-3"><div><h2 className="text-sm font-semibold">Email activity</h2><p className="text-xs text-slate-400 mt-1">Live email rows · SSE {connection}</p></div><span className="text-xs bg-slate-100 rounded px-2 py-1">{visible.length} shown / {unique.length} received</span></div>
    <div className="px-5 pb-4"><input aria-label="Search emails by subject or sender" value={search} onChange={event=>setSearch(event.target.value)} placeholder="Search subject or sender…" className="border rounded-lg px-3 py-2 text-xs outline-none focus:ring-2 focus:ring-teal-300 w-full"/></div>
    <div className="px-5 pb-4 flex flex-wrap items-end gap-3">
      <label className="text-[11px] text-slate-500">IS_ALERT
        <select aria-label="Filter IS_ALERT" value={alertFilter} onChange={event => setAlertFilter(event.target.value)} className="block mt-1 border rounded-lg px-3 py-2 bg-white text-xs text-slate-700">
          {['ALL','YES','NO','UNKNOWN','PENDING'].map(value => <option key={value} value={value}>{value === 'ALL' ? 'All predictions' : value}</option>)}
        </select>
      </label>
      <label className="text-[11px] text-slate-500">IS_5012
        <select aria-label="Filter IS_5012" value={typeFilter} onChange={event => setTypeFilter(event.target.value)} className="block mt-1 border rounded-lg px-3 py-2 bg-white text-xs text-slate-700">
          {['ALL','YES','NO','UNKNOWN','PENDING'].map(value => <option key={value} value={value}>{value === 'ALL' ? 'All predictions' : value}</option>)}
        </select>
      </label>
      <button onClick={() => { setAlertFilter('ALL'); setTypeFilter('ALL'); setSearch(''); }} className="text-xs text-teal-700 px-2 py-2">Reset filters</button>
    </div>
    {error && <p role="alert" className="px-5 pb-3 text-xs text-red-600">{error}</p>}
    <div className="max-h-[560px] overflow-y-auto">
      {!visible.length && <p className="px-5 py-8 text-xs text-slate-400">{!runId ? 'Start a simulation to receive emails.' : unique.length ? 'No emails match this filter.' : 'Waiting for email snapshots…'}</p>}
      {visible.map(row => { const email = row.email || {}; return <button key={row.email_id} onClick={()=>onSelect(row.email_id)} aria-pressed={selectedId===row.email_id} className={`w-full text-left p-4 border-t border-slate-100 ${selectedId===row.email_id ? 'bg-teal-50/60 border-l-2 border-l-teal-600' : 'hover:bg-slate-50'}`}>
        <div className="text-xs font-semibold break-words">{email.subject || '(No subject)'}</div>
        <div className="text-[11px] text-slate-500 mt-1 break-all">{email.sender || '(No sender)'}</div>
        <div className="text-[10px] text-slate-400 mt-1">Received: {receivedLabel(email.timestamp)} SGT</div>
        <div className="text-[10px] text-slate-400 mt-1">Enqueued: {receivedLabel(row.queued_at)} SGT</div>
        <div className="flex flex-wrap gap-1.5 mt-3"><Prediction label="IS_ALERT" value={row.is_alert}/><Prediction label="IS_5012" value={row.is_5012}/></div>
        <div className="flex flex-wrap gap-1.5 mt-2"><StatusTag domain="Classification" status={row.status}/><StatusTag domain="Modem" status={row.modem_status}/><StatusTag domain="Verification" status={row.verification_status}/></div>
        <div className="text-[9px] text-slate-400 font-mono mt-2 break-all">{row.email_id}</div>
      </button>; })}
    </div>
  </section>;
}
