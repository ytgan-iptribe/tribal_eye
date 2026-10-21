import { useEffect, useRef, useState } from "react";

// Leave empty when using the Vite /api proxy.
const API_BASE = (
  import.meta.env.VITE_API_BASE_URL || ""
).replace(/\/$/, "");

const TERMINAL_STATUSES = new Set(["COMPLETED", "FAILED"]);
const RATE_WINDOW_MS = 5000;

export default function useSimulation() {
  const [run, setRun] = useState(null);
  const [starting, setStarting] = useState(false);
  const [connection, setConnection] = useState("idle");
  const [error, setError] = useState("");
  const [ingestionRate, setIngestionRate] = useState(0);

  const mountedRef = useRef(false);
  const startingRef = useRef(false);
  const controllerRef = useRef(null);
  const runRef = useRef(null);

  const rateRef = useRef({
    runId: null,
    previousCount: null,
    samples: [],
    lastRate: 0,
  });

  useEffect(() => {
    mountedRef.current = true;

    return () => {
      mountedRef.current = false;
      controllerRef.current?.abort();
    };
  }, []);

  function refreshRate() {
    const tracker = rateRef.current;
    const cutoff = performance.now() - RATE_WINDOW_MS;

    tracker.samples = tracker.samples.filter(
      (sample) => sample.timestamp > cutoff
    );

    const observedCount = tracker.samples.reduce(
      (total, sample) => total + sample.count,
      0
    );

    const rate = observedCount / (RATE_WINDOW_MS / 1000);

    if (rate !== tracker.lastRate) {
      tracker.lastRate = rate;

      if (mountedRef.current) {
        setIngestionRate(rate);
      }

      console.log("[Simulation] Ingestion Rate", {
        simulation_run_id: tracker.runId,
        emails_per_second: rate,
        window_seconds: RATE_WINDOW_MS / 1000,
      });
    }
  }

  function acceptSnapshot(snapshot) {
    if (!snapshot?.simulation_run_id) {
      throw new Error("Simulation response has no run ID.");
    }

    const count = snapshot.enqueued;

    if (
      typeof count !== "number" ||
      !Number.isFinite(count) ||
      count < 0
    ) {
      throw new Error("Simulation response has an invalid enqueued count.");
    }

    const tracker = rateRef.current;

    if (tracker.runId !== snapshot.simulation_run_id) {
      tracker.runId = snapshot.simulation_run_id;
      tracker.previousCount = null;
      tracker.samples = [];
      tracker.lastRate = 0;

      if (mountedRef.current) {
        setIngestionRate(0);
      }
    }

    const previousCount = tracker.previousCount;

    if (previousCount === null || previousCount !== count) {
      console.log("[Simulation] Total Ingested", {
        simulation_run_id: snapshot.simulation_run_id,
        total_ingested: count,
        total: snapshot.total,
        status: snapshot.status,
      });
    }

    // First snapshot establishes the baseline.
    // Only subsequent increases contribute to the rate.
    if (previousCount !== null && count > previousCount) {
      tracker.samples.push({
        timestamp: performance.now(),
        count: count - previousCount,
      });
    } else if (previousCount !== null && count < previousCount) {
      // Handle a reset without producing a negative rate.
      tracker.samples = [];
    }

    tracker.previousCount = count;
    runRef.current = snapshot;

    if (mountedRef.current) {
      setRun(snapshot);
      setError(snapshot.error || "");
    }

    refreshRate();
  }

  // Continue updating the rate after SSE finishes so it decays to zero.
  useEffect(() => {
    const interval = setInterval(refreshRate, 1000);

    return () => clearInterval(interval);
  }, []);

  const runId = run?.simulation_run_id;
  const finished = TERMINAL_STATUSES.has(run?.status);

  useEffect(() => {
    if (!runId) return;

    if (finished) {
      setConnection("finished");
      return;
    }

    let disposed = false;

    const source = new EventSource(
      `${API_BASE}/api/simulations/${encodeURIComponent(runId)}/stream`
    );

    setConnection("connecting");

    source.onopen = () => {
      if (!disposed) {
        setConnection("connected");
      }
    };

    source.onerror = () => {
      if (disposed) return;

      setConnection("reconnecting");

      // Do not count updates missed during an outage as a new spike.
      rateRef.current.previousCount = null;
      rateRef.current.samples = [];
      refreshRate();

      // EventSource automatically reconnects.
    };

    source.addEventListener("simulation_progress", (event) => {
      if (disposed) return;

      try {
        const snapshot = JSON.parse(event.data);

        if (snapshot.simulation_run_id !== runId) {
          throw new Error("Unexpected simulation run ID.");
        }

        acceptSnapshot(snapshot);

        if (TERMINAL_STATUSES.has(snapshot.status)) {
          disposed = true;
          source.close();
          setConnection("finished");
        }
      } catch (exc) {
        setError(`Invalid simulation update: ${exc.message}`);
      }
    });

    source.addEventListener("unavailable", () => {
      if (disposed) return;

      disposed = true;
      source.close();

      setConnection("unavailable");
      setError(
        "Simulation snapshot is unavailable. Check the backend before retrying."
      );
    });

    return () => {
      disposed = true;
      source.close();
    };
  }, [runId, finished]);

  async function startSimulation() {
    const currentRun = runRef.current;

    if (
      startingRef.current ||
      (currentRun && !TERMINAL_STATUSES.has(currentRun.status))
    ) {
      return;
    }

    startingRef.current = true;
    setStarting(true);
    setError("");

    const controller = new AbortController();
    controllerRef.current = controller;

    const timeout = setTimeout(() => {
      controller.abort();
    }, 15000);

    try {
      const response = await fetch(`${API_BASE}/api/simulations`, {
        method: "POST",
        signal: controller.signal,
      });

      const data = await response.json();

      if (response.status === 409 && data.simulation_run_id) {
        // Attach to the existing producer instead of starting another.
        const existingResponse = await fetch(
          `${API_BASE}/api/simulations/${encodeURIComponent(
            data.simulation_run_id
          )}`,
          { signal: controller.signal }
        );

        const existingRun = await existingResponse.json();

        if (!existingResponse.ok) {
          throw new Error(
            existingRun.error || "Unable to load the existing simulation."
          );
        }

        if (mountedRef.current) {
          acceptSnapshot(existingRun);
        }

        return;
      }

      if (!response.ok) {
        throw new Error(
          data.error || `Simulation request failed: HTTP ${response.status}`
        );
      }

      if (mountedRef.current) {
        acceptSnapshot(data);
      }
    } catch (exc) {
      if (mountedRef.current) {
        setError(
          exc.name === "AbortError"
            ? "Request timed out. The backend may have started the simulation. Check /api/simulations before retrying."
            : exc.message
        );
      }
    } finally {
      clearTimeout(timeout);

      if (controllerRef.current === controller) {
        controllerRef.current = null;
      }

      startingRef.current = false;

      if (mountedRef.current) {
        setStarting(false);
      }
    }
  }

  return {
    run,
    starting,
    connection,
    error,
    startSimulation,

    running:
      starting ||
      Boolean(
        run &&
          !run.ingestion_completed &&
          !TERMINAL_STATUSES.has(run.status)
      ),

    ingested: run?.enqueued ?? 0,
    ingestionRate,
  };
}