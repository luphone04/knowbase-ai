import { describe, expect, it } from "vitest";

import {
  formatLatency,
  formatMs,
  formatScore,
  formatTokens,
  formatUsd,
  isRunActive,
  providerErrorCount,
  requestEstimate,
  runPollInterval,
  runStatus,
  runsPollInterval,
  ratesLabel,
  unmeasuredAttemptsNote,
  usageCost,
  usageIssueText,
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
  const northstar = { query_embeddings_per_run: 26, generations_per_answer_mode: 26 };
  it("uses the API's reservation: embeddings per run, one generation per question and mode", () => {
    expect(requestEstimate(northstar, [])).toEqual({ queryEmbeddings: 26, generations: 0 });
    expect(requestEstimate(northstar, ["hybrid"])).toEqual({ queryEmbeddings: 26, generations: 26 });
    expect(requestEstimate(northstar, ["vector", "hybrid"])).toEqual({
      queryEmbeddings: 26,
      generations: 52,
    });
  });

  it("includes diagnostic follow-up embeddings the API reserves (Asia Cement: 16 + 2)", () => {
    const asia = { query_embeddings_per_run: 18, generations_per_answer_mode: 16 };
    expect(requestEstimate(asia, [])).toEqual({ queryEmbeddings: 18, generations: 0 });
    expect(requestEstimate(asia, ["hybrid"])).toEqual({ queryEmbeddings: 18, generations: 16 });
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

describe("usage and cost formatting", () => {
  it("formats small USD amounts with enough decimals, and nothing for unknown", () => {
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(1.5)).toBe("$1.50");
    expect(formatUsd(0.0123)).toBe("$0.0123");
    expect(formatUsd(0.000468)).toBe("$0.000468");
    expect(formatUsd(0.00000255)).toBe("$0.00000255");
    expect(formatUsd(null)).toBeNull();
    expect(formatUsd(undefined)).toBeNull();
    expect(formatUsd(Number.NaN)).toBeNull();
  });

  it("formats token counts and keeps unknown distinct from zero", () => {
    expect(formatTokens(1234567)).toBe("1,234,567");
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(null)).toBeNull();
  });

  it("shows a cost as unavailable, exact, or a lower bound", () => {
    expect(usageCost({ cost_usd: null, usage_complete: true })).toEqual({ text: "Unavailable", incomplete: false });
    expect(usageCost(undefined).text).toBe("Unavailable");
    expect(usageCost({ cost_usd: 0.0005, usage_complete: true })).toEqual({ text: "$0.000500", incomplete: false });
    expect(usageCost({ cost_usd: 0.0005, usage_complete: false })).toEqual({ text: "≥ $0.000500", incomplete: true });
  });

  it("labels rates and explains issues", () => {
    expect(
      ratesLabel({ kind: "embedding", input_per_million: "0.15", output_per_million: null, cached_input_per_million: null }),
    ).toBe("$0.15 input");
    expect(ratesLabel(null)).toBe("No price recorded");
    expect(usageIssueText({ code: "model_unpriced", kind: "generation", model: "x-model" })).toMatch(
      /No verified price for x-model/,
    );
  });

  it("says Gemini doesn't report embedding tokens, and keeps the failed-call wording for answers", () => {
    expect(usageIssueText({ code: "usage_missing", kind: "embedding", requests: 26 })).toBe(
      "26 query embedding request(s) have no token counts: Gemini doesn't report token usage for embeddings. They are counted as requests but have no estimated cost, so the totals are a lower bound.",
    );
    expect(usageIssueText({ code: "usage_missing", kind: "generation", requests: 1 })).toMatch(
      /1 answer generation request\(s\) returned no token counts \(for example, a failed call\)/,
    );
  });
});

describe("unmeasuredAttemptsNote", () => {
  it("says how many retried attempts weren't measured, or nothing", () => {
    expect(unmeasuredAttemptsNote(0)).toBe("");
    expect(unmeasuredAttemptsNote(undefined)).toBe("");
    expect(unmeasuredAttemptsNote(1)).toBe(" (+1 retried attempt, not measured)");
    expect(unmeasuredAttemptsNote(3)).toBe(" (+3 retried attempts, not measured)");
    expect(
      usageIssueText({ code: "unmeasured_attempts", kind: "generation", attempts: 2 }),
    ).toMatch(/^2 failed answer generation attempt\(s\) were retried/);
  });
});
