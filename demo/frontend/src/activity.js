export const ACTIVITY_VIEWS = {
  "30s": { label: "Last 30 seconds", duration: 30000, interval: 250 },
  "1m": { label: "Last 1 minute", duration: 60000, interval: 250 },
  "5m": { label: "Last 5 minutes", duration: 300000, interval: 5000 },
};
export function buildActivity(rows, now, view = "30s") {
  const { duration, interval } = ACTIVITY_VIEWS[view] || ACTIVITY_VIEWS["30s"];
  const end = Math.floor(now / interval) * interval;
  const start = end - duration + interval;
  const points = Array.from({ length: duration / interval }, (_, i) => ({ at: start + i * interval, inbound: 0, processed: 0 }));
  const terminal = new Set(['COMPLETED_CLASSIFICATION', 'COMPLETED_CLASSIFICATION_WORKER', 'FAILED_CLASSIFICATION', 'FAILED_CLASSIFICATION_WORKER']);
  const unique = new Map(rows.filter(row => row.email_id).map(row => [row.email_id, row]));
  function add(value, field) {
    if (typeof value !== 'string' || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) return;
    const at = Date.parse(value);
    if (!Number.isFinite(at) || at > now || at < start) return;
    const index = Math.floor((at - start) / interval);
    if (points[index]) points[index][field]++;
  }
  for (const row of unique.values()) {
    add(row.queued_at || row.email?.ingested_at, 'inbound');
    if (terminal.has(row.status)) add(row.finished_at, 'processed');
  }
  return { points, interval };
}
