import { useEffect, useState } from "react";
import { classificationLatency } from "./metrics";
const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
export default function useEmailMetrics(runId) {
  const [snapshot, setSnapshot] = useState({ runId: null, rows: [] });
  const [connection, setConnection] = useState("idle");
  const [error, setError] = useState("");
  useEffect(() => {
    setError("");
    if (!runId) { setConnection("idle"); return; }
    let disposed = false;
    const source = new EventSource(`${API_BASE}/api/simulations/${encodeURIComponent(runId)}/emails/stream`);
    setConnection("connecting");
    source.onopen = () => { if (!disposed) setConnection("connected"); };
    source.onerror = () => { if (!disposed) setConnection("reconnecting"); };
    source.addEventListener("emails_progress", event => {
      if (disposed) return;
      try {
        const value = JSON.parse(event.data);
        if (value.simulation_run_id !== runId || !Array.isArray(value.emails)) throw new Error("Invalid email snapshot");
        // Full snapshots replace rows, so repeat delivery does not double count.
        const unique = new Map(value.emails.filter(row => row.email_id).map(row => [row.email_id, row]));
        setSnapshot({ runId, rows: [...unique.values()] });
        setError("");
      } catch (exc) { setError(exc.message); }
    });
    source.addEventListener("unavailable", () => {
      if (disposed) return;
      disposed = true; source.close(); setConnection("unavailable"); setError("Email metrics unavailable");
    });
    return () => { disposed = true; source.close(); };
  }, [runId]);
  const rows = snapshot.runId === runId ? snapshot.rows : [];
  return { ...classificationLatency(rows), rows, connection, error };
}
