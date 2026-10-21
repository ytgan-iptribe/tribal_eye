import React, { useEffect, useState } from 'react';
import { dagStates, durationLabel } from './dag';
import ExecutionTelemetry from './ExecutionTelemetry';
const styles = {
  queued: 'bg-slate-100 text-slate-500 border-slate-200',
  executing: 'bg-blue-50 text-blue-700 border-blue-300',
  completed: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  failed: 'bg-red-50 text-red-700 border-red-200',
  not_applicable: 'bg-violet-50 text-violet-600 border-violet-200',
};
const labels = { queued:'Queued / not started', executing:'Executing', completed:'Completed', failed:'Failed', not_applicable:'Not applicable' };
function DagNode({ title, state, prediction, message, duration }) {
  return <div className={`rounded-lg border p-3 min-w-0 ${styles[state]}`}><h3 className="text-xs font-semibold flex gap-2 items-center"><span className={state==='executing'?'pulse':''}>●</span>{title}</h3><p className="text-[10px] mt-2">{labels[state]}{duration ? ` · ${duration}` : ''}</p>{prediction && <p className="text-xs font-semibold mt-1">Prediction: {prediction}</p>}{message && <p className="text-[10px] mt-1 break-words">{message}</p>}</div>;
}
export default function EmailDagPanel({ row, selectedId, connection }) {
  const [now, setNow] = useState(Date.now());
  const [showBody, setShowBody] = useState(false);
  useEffect(() => { const timer=setInterval(()=>setNow(Date.now()),250);return()=>clearInterval(timer); },[]);
  useEffect(()=>setShowBody(false),[selectedId]);
  if (!row) return <section className="card p-5"><h2 className="text-sm font-semibold">Dynamic execution DAG</h2><p className="text-xs text-slate-400 mt-4">{selectedId ? 'Selected email is no longer in the current snapshot.' : 'Select an email to inspect its live workflow.'}</p></section>;
  const states=dagStates(row), email=row.email||{};
  const classTime=durationLabel(row.started_at,row.finished_at,now);
  return <section className="card p-5 min-w-0">
    <p className="text-[10px] text-teal-700 uppercase tracking-widest">Live execution trace · SSE {connection}</p>
    <h2 className="text-sm font-semibold mt-2 break-words">{email.subject || '(No subject)'}</h2>
    <p className="font-mono text-[9px] text-slate-400 mt-2 break-all">{row.email_id}</p>
    <button className="text-xs text-teal-700 mt-3" onClick={()=>setShowBody(!showBody)}>{showBody?'Hide raw email':'View raw email'}</button>
    {showBody && <pre className="whitespace-pre-wrap break-words bg-slate-50 border rounded-lg p-3 text-xs mt-3 max-h-64 overflow-auto">{`Sender: ${email.sender || ''}\nReceived: ${email.timestamp || ''}\n\n${email.body || '(No body)'}`}</pre>}
    <div className="flex flex-wrap gap-3 text-[10px] mt-5 mb-4"><span className="text-slate-400">● Queued</span><span className="text-blue-600">● Executing</span><span className="text-emerald-600">● Completed</span><span className="text-red-600">● Failed</span><span className="text-violet-600">● Not applicable</span></div>
    <div className="grid grid-cols-1 sm:grid-cols-[1fr_16px_1fr_16px_1fr] items-center gap-1">
      <DagNode title="Raw email" state={states.raw}/><span className="text-slate-300 text-center">→</span>
      <DagNode title="IS_ALERT" state={states.alert} prediction={row.is_alert || 'PENDING'} duration={classTime}/><span className="text-slate-300 text-center">→</span>
      <DagNode title="IS_5012" state={states.type} prediction={row.is_5012 || 'PENDING'} duration={classTime}/>
    </div>
    <p className="text-[10px] text-slate-400 mt-2">Both decisions share the classification lifecycle and timer.</p>
    <div className="flex justify-center text-slate-300 my-3">↓ Parallel branches ↓</div>
    <div className="grid sm:grid-cols-2 gap-4">
      <DagNode title="Verification" state={states.verification} duration={durationLabel(row.verification_started_at,row.verification_finished_at,now)} message={states.verification==='not_applicable'?'This email does not enter verification.':row.verification_message || row.verification_status}/>
      <DagNode title="Modem checks" state={states.modem} duration={durationLabel(row.modem_started_at,row.modem_finished_at,now)} message={states.modem==='not_applicable'?'This email does not enter modem checks.':row.modem_message || row.modem_status}/>
    </div>
    <p className="text-[10px] text-slate-400 mt-4">Green means execution completed, not incident resolved. Substeps may be brief and coalesced by snapshot SSE.</p>
    <ExecutionTelemetry key={row.email_id} emailId={row.email_id} row={row}/>
  </section>;
}
