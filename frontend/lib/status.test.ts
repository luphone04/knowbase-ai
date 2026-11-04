import { describe, expect, it } from "vitest";

import {
  POLL_INTERVAL_MS,
  jobStatus,
  sourceDetailPollInterval,
  sourceStatus,
  sourcesPollInterval,
} from "./status";
import type { Job, Source, SourceDetail, SourceStatus } from "./types";

function source(status: SourceStatus): Source {
  return {
    id: `id-${status}`,
    kind: "file",
    display_name: "returns-policy.md",
    original_filename: "returns-policy.md",
    file_type: "md",
    file_size: 100,
    status,
    error_message: null,
    last_indexed_at: null,
    created_at: "2026-11-04T00:00:00Z",
    updated_at: "2026-11-04T00:00:00Z",
    document_count: 0,
    chunk_count: 0,
    latest_job: null,
  };
}

function job(status: Job["status"], attempts: number): Job {
  return {
    id: "job",
    source_id: "id",
    job_type: "ingest",
    status,
    attempts,
    max_attempts: 3,
    next_run_at: "2026-11-04T00:00:00Z",
    last_error: null,
    result: {},
    created_at: "2026-11-04T00:00:00Z",
    started_at: null,
    finished_at: null,
  };
}

describe("sourcesPollInterval", () => {
  it("polls while any source is pending, processing, or deleting", () => {
    for (const status of ["pending", "processing", "deleting"] as const) {
      expect(sourcesPollInterval([source("ready"), source(status)])).toBe(POLL_INTERVAL_MS);
    }
  });

  it("stops when every source is ready or failed, or before data arrives", () => {
    expect(sourcesPollInterval([source("ready"), source("failed")])).toBe(false);
    expect(sourcesPollInterval([])).toBe(false);
    expect(sourcesPollInterval(undefined)).toBe(false);
  });
});

describe("sourceDetailPollInterval", () => {
  it("polls while the source or one of its jobs is active", () => {
    const ready: SourceDetail = { ...source("ready"), documents: [], jobs: [job("succeeded", 1)] };
    expect(sourceDetailPollInterval(ready)).toBe(false);
    expect(sourceDetailPollInterval({ ...ready, jobs: [job("queued", 0)] })).toBe(
      POLL_INTERVAL_MS,
    );
    expect(sourceDetailPollInterval({ ...ready, status: "processing" })).toBe(POLL_INTERVAL_MS);
  });
});

describe("labels", () => {
  it("never shows Ready unless the worker marked the source ready", () => {
    expect(sourceStatus("pending").label).toBe("Queued");
    expect(sourceStatus("processing").label).toBe("Processing");
    expect(sourceStatus("ready").label).toBe("Ready");
  });

  it("shows attempts and retries", () => {
    expect(jobStatus(job("queued", 0)).label).toBe("Queued");
    expect(jobStatus(job("running", 1)).label).toBe("Running (attempt 1 of 3)");
    expect(jobStatus(job("queued", 1)).label).toBe("Waiting to retry (attempt 2 of 3)");
    expect(jobStatus(job("failed", 3)).tone).toBe("danger");
  });
});
