export function stateFromStatus(status) {
  if (!status) return 'queued';
  if (status.startsWith('EXECUTING')) return 'executing';
  if (status.startsWith('FAILED')) return 'failed';
  if (/^COMPLETE(?:D)?_/.test(status)) return 'completed';
  return 'queued';
}
export function dagStates(row) {
  if (!row) return null;
  const classification = stateFromStatus(row.status);
  const hasDispatch = row.routed_actionable === true || row.routed_actionable === 1;
  const notApplicable = classification === 'completed' && !hasDispatch && (row.is_alert === 'NO' || row.is_5012 === 'NO');
  const branch = status => status ? stateFromStatus(status) : notApplicable ? 'not_applicable' : 'queued';
  return { raw: 'completed', alert: classification, type: classification,
    verification: branch(row.verification_status), modem: branch(row.modem_status) };
}
export function durationLabel(start, finish, now) {
  if (!start) return '';
  const beginning = Date.parse(start), ending = finish ? Date.parse(finish) : now;
  if (!Number.isFinite(beginning) || !Number.isFinite(ending) || ending < beginning) return '';
  const seconds = (ending - beginning) / 1000;
  return seconds < 1 ? `${Math.round(seconds * 1000)} ms` : seconds < 60 ? `${seconds.toFixed(1)} s` : `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}
