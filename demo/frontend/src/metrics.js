const COMPLETED = new Set(["COMPLETED_CLASSIFICATION", "COMPLETED_CLASSIFICATION_WORKER"]);
const timestamp = value => typeof value === "string" && /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? Date.parse(value) : NaN;
export function classificationLatency(rows) {
  const totals = { total: 0, wait: 0, processing: 0, count: 0, waitCount: 0, processingCount: 0 };
  for (const row of rows) {
    if (!COMPLETED.has(row.status)) continue;
    const queued = timestamp(row.queued_at), started = timestamp(row.started_at), finished = timestamp(row.finished_at);
    if (!Number.isFinite(queued) || !Number.isFinite(finished) || finished < queued) continue;
    totals.total += finished - queued; totals.count++;
    if (Number.isFinite(started) && started >= queued && started <= finished) {
      totals.wait += started - queued; totals.waitCount++;
      totals.processing += finished - started; totals.processingCount++;
    }
  }
  return { averageMs: totals.count ? totals.total / totals.count : null,
    queueWaitMs: totals.waitCount ? totals.wait / totals.waitCount : null,
    processingMs: totals.processingCount ? totals.processing / totals.processingCount : null,
    sampleCount: totals.count };
}
export function noiseMetrics(run) {
  const ingested = Number(run?.enqueued ?? 0);
  const filtered = Number(run?.total_filtered ?? 0);
  return { filtered, noisePercent: ingested > 0 ? filtered / ingested * 100 : null,
    estimatedHoursSaved: filtered * 2 / 60 };
}
export const formatMs = value => value === null ? "—" : value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;

// Elapsed time and active effort are distinct, adjustable assumptions.
export const MANUAL_TIMING = {
  classifyAlert: 30,
  classify5012: 15,
  verification: 900,
  modemExecution: 205,
  modemHandling: 60,
  finalReview: 60,
  dashboardHandsOn: 120,
  modemHandsOn: 120,
  compilationHandsOn: 60,
};
export function timeSavings(rows, assumptions = MANUAL_TIMING, hourlyRate = 25, assumedReviewSeconds = 0) {
  const rate = Number.isFinite(hourlyRate) && hourlyRate >= 0 ? hourlyRate : 25;
  const fallbackReview = Number.isFinite(assumedReviewSeconds) && assumedReviewSeconds >= 0 ? assumedReviewSeconds : 0;
  const unique = new Map(rows.filter(row => row.email_id).map(row => [row.email_id, row]));
  const results = [...unique.values()].map(row => {
    const base = { emailId: row.email_id, manualSeconds: null, automatedSeconds: null,
      savedSeconds: null, reclaimedSeconds: null, reallocatedValue: null };
    if (!COMPLETED.has(row.status) || !["YES", "NO"].includes(row.is_alert)) return { ...base, state: "awaiting_classification" };
    let classify = assumptions.classifyAlert;
    if (row.is_alert === "YES") {
      if (!["YES", "NO"].includes(row.is_5012)) return { ...base, state: "review_required" };
      classify += assumptions.classify5012;
    }
    const actionable = row.routed_actionable === true || row.routed_actionable === 1;
    const manual = classify + (actionable ? Math.max(assumptions.verification,
      assumptions.modemExecution + assumptions.modemHandling) + assumptions.finalReview : 0);
    let end = timestamp(row.finished_at);
    if (actionable) {
      if (row.modem_status === "FAILED_MODEM" || row.verification_status === "FAILED_VERIFICATION") return { ...base, manualSeconds: manual, state: "review_required" };
      if (row.modem_status !== "COMPLETED_MODEM" || !["COMPLETE_VERIFICATION", "COMPLETED_VERIFICATION"].includes(row.verification_status)) return { ...base, manualSeconds: manual, state: "awaiting_workflows" };
      const modemEnd = timestamp(row.modem_finished_at), verificationEnd = timestamp(row.verification_finished_at);
      if (!Number.isFinite(modemEnd) || !Number.isFinite(verificationEnd)) return { ...base, manualSeconds: manual, state: "missing_timestamps" };
      end = Math.max(end, modemEnd, verificationEnd);
    }
    const start = timestamp(row.queued_at);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return { ...base, manualSeconds: manual, state: "missing_timestamps" };
    const automated = (end - start) / 1000;
    const handsOn = classify + (actionable ? assumptions.dashboardHandsOn + assumptions.modemHandsOn + assumptions.compilationHandsOn : 0);
    // Optional future backend field; otherwise use the explicitly displayed assumption.
    const recordedReview = row.human_review_seconds;
    const hasRecordedReview = typeof recordedReview === "number" && Number.isFinite(recordedReview) && recordedReview >= 0;
    const review = hasRecordedReview ? recordedReview : fallbackReview;
    const needsReview = row.review_required === true || row.modem_all_checks_passed === false || Boolean(row.verification_error || row.modem_error);
    const reclaimed = handsOn - review;
    return { ...base, state: "estimated", manualSeconds: manual, automatedSeconds: automated,
      savedSeconds: manual - automated, manualHandsOnSeconds: handsOn, reviewSeconds: review,
      reviewSource: hasRecordedReview ? "recorded" : "assumed",
      reclaimedSeconds: needsReview ? null : reclaimed,
      reallocatedValue: needsReview ? null : reclaimed / 3600 * rate,
      valueState: needsReview ? "review_required" : "estimated" };
  });
  const eligible = results.filter(row => row.state === "estimated");
  const valued = eligible.filter(row => row.reallocatedValue !== null);
  return { results, savedSeconds: eligible.reduce((sum,row) => sum + row.savedSeconds,0),
    measuredEmails: eligible.length, pendingEmails: results.length - eligible.length,
    reallocatedValue: valued.reduce((sum,row) => sum + row.reallocatedValue,0),
    valuedEmails: valued.length, excludedValueEmails: results.length - valued.length };
}
