import React, { useEffect, useRef, useState } from 'react';
import useExecutionLogs from './useExecutionLogs';

const CHECKS = [
  ['data', 'Data connectivity'],
  ['sms_z2v', 'SMS · Zero1 → VIVIFI'],
  ['sms_v2z', 'SMS · VIVIFI → Zero1'],
  ['call_z2v', 'Call · Zero1 → VIVIFI'],
  ['call_v2z', 'Call · VIVIFI → Zero1'],
];
function Output({ rows, follow }) {
  const box = useRef(null);
  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [rows, follow]);
  return <div ref={box} className="max-h-72 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-5">
    {!rows.length && <p className="text-slate-400">No output received yet.</p>}
    {rows.map((row, index) => <div key={row.cursor ?? row.event_id ?? index} className="mb-2">
      <div className="text-slate-500 text-[9px]">{row.timestamp} · {row.stream || row.event || 'status'}
        {row.check_number ? ` · check ${row.check_number}` : ''}{row.attempt ? ` · attempt ${row.attempt}` : ''}
      </div>
      <pre className={`whitespace-pre-wrap break-words ${row.stream === 'stderr' || row.event === 'STDERR' ? 'text-red-300' : row.kind === 'status' ? 'text-blue-300' : 'text-slate-100'}`}>{row.message ?? ''}</pre>
    </div>)}
  </div>;
}
export default function ExecutionTelemetry({ emailId, row }) {
  const verification = useExecutionLogs(emailId, 'verification');
  const modem = useExecutionLogs(emailId, 'modem');
  const [follow, setFollow] = useState(true);
  return <div className="border-t mt-5 pt-4 space-y-4">
    <div className="flex items-center justify-between"><h3 className="text-sm font-semibold">Live action telemetry</h3>
      <label className="text-xs text-slate-500 flex gap-2"><input type="checkbox" checked={follow} onChange={e => setFollow(e.target.checked)}/>Follow output</label>
    </div>
    <section className="space-y-2">
      <h4 className="text-xs font-semibold">Verification · {verification.connection}</h4>
      <p className="text-[10px] text-blue-700 break-words">{row.verification_status || 'Not started'} · {row.verification_message || ''}</p>
      {verification.error && <p className="text-xs text-amber-700">{verification.error}</p>}
      <p className="text-[10px] text-slate-500">Live Python observations and PowerShell stdout/stderr. Simulation observations appear here even when PowerShell is not executed.</p>
      <Output rows={verification.rows} follow={follow}/>
      {verification.dropped > 0 && <p className="text-[10px] text-slate-500">Showing the most recent 2,000 records.</p>}
    </section>
    <section className="space-y-2">
      <h4 className="text-xs font-semibold">Modem checks · {modem.connection}</h4>
      <p className="text-[10px] text-blue-700 break-words">{row.modem_status || 'Not started'} · {row.modem_message || ''}</p>
      {modem.error && <p className="text-xs text-amber-700">{modem.error}</p>}
      <details open className="border rounded-lg p-2"><summary className="cursor-pointer text-xs mb-2">SSH session and workflow events</summary>
        <Output rows={modem.rows.filter(event => !event.step || !CHECKS.some(([key]) => key === event.step))} follow={follow}/>
      </details>
      {CHECKS.map(([step, title]) => <details key={step} open={row.modem_step === step} className="border rounded-lg p-2">
        <summary className="cursor-pointer text-xs mb-2">{title}{row.modem_step === step ? ' · current check' : ''}</summary>
        <Output rows={modem.rows.filter(event => event.step === step)} follow={follow}/>
      </details>)}
      {modem.dropped > 0 && <p className="text-[10px] text-slate-500">Showing the most recent 2,000 records across checks.</p>}
    </section>
  </div>;
}
