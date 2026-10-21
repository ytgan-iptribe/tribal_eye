import { useEffect, useState } from 'react';

const API = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');
const LIMIT = 2000;
export default function useExecutionLogs(emailId, domain) {
  const [snapshot, setSnapshot] = useState({ emailId: null, rows: [], connection: 'idle', error: null, dropped: 0 });
  useEffect(() => {
    if (!emailId) return;
    let disposed = false;
    const seen = new Set();
    const path = domain === 'modem' ? 'modem-events/stream' : 'verification-logs/stream';
    setSnapshot({ emailId, rows: [], connection: 'connecting', error: null, dropped: 0 });
    const source = new EventSource(`${API}/api/emails/${encodeURIComponent(emailId)}/${path}`);
    const receive = event => {
      if (disposed) return;
      try {
        const row = JSON.parse(event.data);
        if (row.email_id && row.email_id !== emailId) return;
        const key = row.cursor ?? event.lastEventId ?? row.event_id;
        if (key !== undefined && key !== '' && seen.has(String(key))) return;
        if (key !== undefined && key !== '') {
          seen.add(String(key));
          if (seen.size > LIMIT * 2) seen.delete(seen.values().next().value);
        }
        setSnapshot(old => {
          const rows = [...old.rows, row];
          const excess = Math.max(0, rows.length - LIMIT);
          return { ...old, rows: rows.slice(-LIMIT), dropped: old.dropped + excess, error: null };
        });
      } catch {
        setSnapshot(old => ({ ...old, error: 'Invalid telemetry record received.' }));
      }
    };
    const names = domain === 'modem' ? ['modem_log', 'modem_status'] : ['verification_log'];
    names.forEach(name => source.addEventListener(name, receive));
    source.onopen = () => !disposed && setSnapshot(old => ({ ...old, connection: 'connected', error: null }));
    source.onerror = () => !disposed && setSnapshot(old => ({ ...old, connection: 'reconnecting', error: 'Stream disconnected; retrying automatically.' }));
    return () => { disposed = true; source.close(); };
  }, [emailId, domain]);
  return snapshot.emailId === emailId ? snapshot : { rows: [], connection: 'connecting', error: null, dropped: 0 };
}
