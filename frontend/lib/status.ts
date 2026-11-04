// Labels for source and job states, and when to poll. The worker owns these states:
// nothing here infers progress the API did not report.

import type { Job, JobType, Source, SourceDetail, SourceStatus } from "./types";

export type Tone = "neutral" | "info" | "success" | "danger" | "warning";

const SOURCE_STATUS: Record<SourceStatus, { label: string; tone: Tone }> = {
  pending: { label: "Queued", tone: "neutral" },
  processing: { label: "Processing", tone: "info" },
  ready: { label: "Ready", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  deleting: { label: "Deleting", tone: "warning" },
};

export function sourceStatus(status: SourceStatus): { label: string; tone: Tone } {
  return SOURCE_STATUS[status] ?? { label: status, tone: "neutral" };
}

const ACTIVE_SOURCE_STATUSES: ReadonlySet<SourceStatus> = new Set([
  "pending",
  "processing",
  "deleting",
]);

/** The worker still has work to do for this source. */
export function isSourceActive(source: Pick<Source, "status">): boolean {
  return ACTIVE_SOURCE_STATUSES.has(source.status);
}

export function isJobActive(job: Pick<Job, "status">): boolean {
  return job.status === "queued" || job.status === "running";
}

export const POLL_INTERVAL_MS = 3000;

/** Poll the list while any source is still changing; stop once all are settled. */
export function sourcesPollInterval(sources: Source[] | undefined): number | false {
  return sources?.some(isSourceActive) ? POLL_INTERVAL_MS : false;
}

export function sourceDetailPollInterval(detail: SourceDetail | undefined): number | false {
  if (!detail) return false;
  return isSourceActive(detail) || detail.jobs.some(isJobActive) ? POLL_INTERVAL_MS : false;
}

const JOB_TYPE: Record<JobType, string> = {
  ingest: "Index",
  reindex: "Re-index",
  delete: "Delete",
};

export function jobTypeLabel(type: JobType): string {
  return JOB_TYPE[type] ?? type;
}

/** "Running (attempt 1 of 3)", "Waiting to retry (attempt 2 of 3)", ... */
export function jobStatus(job: Job): { label: string; tone: Tone } {
  switch (job.status) {
    case "queued":
      return job.attempts > 0
        ? {
            label: `Waiting to retry (attempt ${job.attempts + 1} of ${job.max_attempts})`,
            tone: "warning",
          }
        : { label: "Queued", tone: "neutral" };
    case "running":
      return {
        label: `Running (attempt ${Math.max(job.attempts, 1)} of ${job.max_attempts})`,
        tone: "info",
      };
    case "succeeded":
      return { label: "Succeeded", tone: "success" };
    case "failed":
      return { label: "Failed", tone: "danger" };
    default:
      return { label: job.status, tone: "neutral" };
  }
}
