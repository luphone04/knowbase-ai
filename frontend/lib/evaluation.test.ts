import { describe, expect, it } from "vitest";

import {
  formatLatency,
  formatMs,
  formatScore,
  isRunActive,
  providerErrorCount,
  requestEstimate,
  runPollInterval,
  runStatus,
  runsPollInterval,
  variantDetails,
} from "./evaluation";
import type { EvaluationRun } from "./types";

describe("formatScore", () => {
  it("keeps zero distinct from not measured", () => {
    expect(formatScore(0)).toBe("0.00");
    expect(formatScore(0.6667)).toBe("0.67");
    expect(formatScore(1)).toBe("1.00");
    expect(formatScore(null)).toBeNull();
    expect(formatScore(undefined)).toBeNull();
    expect(formatScore(Number.NaN)).toBeNull();
  });
});

describe("latency", () => {
  it("formats milliseconds and seconds", () => {
    expect(formatMs(412.4)).toBe("412 ms");
    expect(formatMs(12_345)).toBe("12.3 s");
    expect(formatMs(null)).toBe("—");
  });

  it("summarises the median and p95, or nothing", () => {
    expect(formatLatency({ count: 26, min: 90, median: 120, p95: 340, max: 400 })).toBe(
      "median 120 ms · p95 340 ms (n = 26)",
    );
    expect(formatLatency(null)).toBeNull();
    expect(formatLatency(undefined)).toBeNull();
  });
});

describe("run status and polling", () => {
  it("polls only while a run is queued or running", () => {
    expect(runPollInterval({ status: "queued" })).toBe(4000);
    expect(runPollInterval({ status: "running" })).toBe(4000);
    expect(runPollInterval({ status: "succeeded" })).toBe(false);
    expect(runPollInterval({ status: "failed" })).toBe(false);
    expect(runPollInterval(undefined)).toBe(false);
    expect(runsPollInterval([{ status: "succeeded" }, { status: "running" }])).toBe(4000);
    expect(runsPollInterval([{ status: "failed" }])).toBe(false);
    expect(runsPollInterval(undefined)).toBe(false);
  });

  it("labels runs without calling a stopped run complete", () => {
    expect(runStatus({ status: "succeeded" })).toEqual({ label: "Completed", tone: "success" });
    expect(runStatus({ status: "failed" })).toEqual({ label: "Stopped", tone: "danger" });
    expect(isRunActive({ status: "failed" })).toBe(false);
  });
});

describe("requestEstimate", () => {
  it("counts one embedding per question and one generation per question and mode", () => {
    expect(requestEstimate(26, [])).toEqual({ queryEmbeddings: 26, generations: 0 });
    expect(requestEstimate(26, ["hybrid"])).toEqual({ queryEmbeddings: 26, generations: 26 });
    expect(requestEstimate(26, ["vector", "hybrid"])).toEqual({
      queryEmbeddings: 26,
      generations: 52,
    });
  });
});

describe("providerErrorCount", () => {
  it("adds failed embeddings and failed answers", () => {
    const run = {
      summary: {
        retrieval: { vector: { errors: 1 } },
        answers: { hybrid: { provider_errors: 2 }, vector: { provider_errors: 1 } },
      },
    } as unknown as EvaluationRun;
    expect(providerErrorCount(run)).toBe(4);
    expect(providerErrorCount({ summary: {} } as EvaluationRun)).toBe(0);
  });
});

describe("variantDetails", () => {
  it("uses the labels recorded with the run, with a fallback", () => {
    const run = {
      config: {
        variant_details: {
          hybrid_or: {
            label: "Hybrid: keyword OR, simple",
            mode: "hybrid",
            experimental: true,
            lexical_config: "simple",
            lexical_match: "any",
          },
        },
      },
    } as unknown as EvaluationRun;
    expect(variantDetails(run, "hybrid_or").label).toBe("Hybrid: keyword OR, simple");
    expect(variantDetails(run, "vector")).toMatchObject({ label: "vector", experimental: false });
    expect(variantDetails(run, "hybrid_english")).toMatchObject({ experimental: true });
  });
});
