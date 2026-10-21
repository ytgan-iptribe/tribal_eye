import React, { useState } from 'react';
import { timeSavings } from './metrics';
const money = value => new Intl.NumberFormat('en-SG', { style: 'currency', currency: 'SGD' }).format(value);
const minutes = value => value == null ? '—' : `${(value / 60).toFixed(2)}m`;
export default function AutomationImpact({ rows }) {
  const [rate, setRate] = useState(25);
  const [reviewMinutes, setReviewMinutes] = useState(0);
  const impact = timeSavings(rows, undefined, rate, reviewMinutes * 60);
  return <section className="card p-5 mb-5">
    <div className="grid sm:grid-cols-2 gap-5">
      <div><h2 className="text-sm font-semibold">Estimated workflow time saved</h2><strong className="block text-2xl mt-2">{impact.measuredEmails ? `${(impact.savedSeconds / 3600).toFixed(2)} h` : '—'}</strong><p className="text-xs text-slate-500 mt-2">Manual parallel workflow minus actual automated elapsed time, including queue wait.</p><p className="text-xs text-slate-400 mt-1">{impact.measuredEmails} calculated · {impact.pendingEmails} pending / unavailable</p></div>
      <div><h2 className="text-sm font-semibold">Estimated value of reallocated time</h2><strong className="block text-2xl mt-2">{impact.valuedEmails ? money(impact.reallocatedValue) : '—'}</strong><p className="text-xs text-slate-500 mt-2">Hands-on effort avoided × SGD {rate}/hour. Productivity value, not cash savings.</p><p className="text-xs text-slate-400 mt-1">{impact.valuedEmails} eligible · {impact.excludedValueEmails} pending / excluded</p></div>
    </div>
    <details className="mt-4 text-xs"><summary className="cursor-pointer text-teal-700">Calculation assumptions and per-email breakdown</summary>
      <div className="flex flex-wrap gap-5 my-4"><label>Blended rate (SGD/hour)<input type="number" min="0" step="1" value={rate} onChange={e => setRate(Math.max(0, Number(e.target.value) || 0))} className="block border rounded px-2 py-1 mt-1 w-28"/></label><label>Assumed post-automation review (minutes/email)<input type="number" min="0" step="0.5" value={reviewMinutes} onChange={e => setReviewMinutes(Math.max(0, Number(e.target.value) || 0))} className="block border rounded px-2 py-1 mt-1 w-28"/></label></div>
      <p className="text-slate-500 leading-5">Manual elapsed: 30s alert classification, plus 15s type classification for alerts. For actionable emails add max(15m verification, 3m25s modem execution + 1m handling), then 1m final review. Both manual and automated branches may run in parallel.</p>
      <p className="text-slate-500 leading-5 mt-2">Manual hands-on: 30s for non-alerts, 45s for other alerts, 5m45s for actionable alerts. Subtract the assumed review above, or recorded human_review_seconds when supplied. These are adjustable estimates in metrics.js, not measured operator effort.</p>
      <p className="text-amber-700 mt-2">{reviewMinutes === 0 ? 'Assumes no additional human review unless recorded.' : `Assumes ${reviewMinutes} minutes of review per email unless recorded.`} Completed verification does not prove resolution. Known failures and review flags are excluded from monetary value; unrecorded interventions may still overstate it.</p>
      <div className="overflow-auto max-h-64 mt-4"><table className="w-full text-left"><thead><tr><th className="p-2">Email</th><th>Manual elapsed</th><th>Automated elapsed</th><th>Time saved</th><th>Reallocated value</th></tr></thead><tbody>{impact.results.map(item => <tr key={item.emailId} className="border-t"><td className="p-2 font-mono">{item.emailId}</td><td>{minutes(item.manualSeconds)}</td><td>{minutes(item.automatedSeconds)}</td><td>{item.savedSeconds == null ? item.state.replaceAll('_',' ') : minutes(item.savedSeconds)}</td><td>{item.reallocatedValue == null ? 'Excluded / pending' : money(item.reallocatedValue)}</td></tr>)}</tbody></table></div>
    </details>
  </section>;
}
