// Labels, formatting, and polling for evaluation runs. Every number shown comes from the
// API's stored results; a metric that wasn't measured is null and is shown as such, never
// as 0.

import type { Tone } from "./status";
import type {
  AnswerMode,
  CorpusSourceStatus,
  EvaluationOutcome,
  EvaluationResult,
  EvaluationRun,
  EvaluationRunStatus,
  LatencyStats,
  VariantDetails,
} from "./types";

export const NOT_MEASURED = "Not measured";

/** A 0–1 metric with two decimals, or null when it wasn't measured. */
export function formatScore(value: number | null | undefined): string | null {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : null;
}

export function formatMs(value: number | null | undefined): string {
  if (typeof value !== "number") return "—";
  return value >= 10_000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
}

/** "median 120 ms · p95 340 ms (n = 26)", or null when nothing was measured. */
export function formatLatency(stats: LatencyStats | undefined): string | null {
  if (!stats) return null;
  return `median ${formatMs(stats.median)} · p95 ${formatMs(stats.p95)} (n = ${stats.count})`;
}

const RUN_STATUS: Record<EvaluationRunStatus, { label: string; tone: Tone }> = {
  queued: { label: "Queued", tone: "neutral" },
  running: { label: "Running", tone: "info" },
  succeeded: { label: "Completed", tone: "success" },
  failed: { label: "Stopped", tone: "danger" },
};

export function runStatus(run: Pick<EvaluationRun, "status">): { label: string; tone: Tone } {
  return RUN_STATUS[run.status] ?? { label: run.status, tone: "neutral" };
}

export function isRunActive(run: Pick<EvaluationRun, "status">): boolean {
  return run.status === "queued" || run.status === "running";
}

export const RUN_POLL_INTERVAL_MS = 4000;

/** Poll while a run is queued or running; stop once it has finished. */
export function runPollInterval(run: Pick<EvaluationRun, "status"> | undefined): number | false {
  return run && isRunActive(run) ? RUN_POLL_INTERVAL_MS : false;
}

export function runsPollInterval(runs: Pick<EvaluationRun, "status">[] | undefined): number | false {
  return runs?.some(isRunActive) ? RUN_POLL_INTERVAL_MS : false;
}

/** Gemini requests a run will reserve: one query embedding per question, one answer per mode. */
export function requestEstimate(questions: number, modes: AnswerMode[]) {
  return { queryEmbeddings: questions, generations: questions * modes.length };
}

/** Provider failures across the run: failed query embeddings and failed answers. */
export function providerErrorCount(run: Pick<EvaluationRun, "summary">): number {
  const summary = run.summary;
  const embedding = summary.retrieval?.vector?.errors ?? 0;
  const answers = Object.values(summary.answers ?? {}).reduce(
    (total, mode) => total + (mode?.provider_errors ?? 0),
    0,
  );
  return embedding + answers;
}

const CORPUS_STATUS: Record<CorpusSourceStatus, { label: string; tone: Tone }> = {
  ready: { label: "Ready", tone: "success" },
  missing: { label: "Not uploaded", tone: "danger" },
  not_ready: { label: "Not indexed yet", tone: "warning" },
  content_differs: { label: "Content differs", tone: "danger" },
};

export function corpusStatus(status: CorpusSourceStatus): { label: string; tone: Tone } {
  return CORPUS_STATUS[status] ?? { label: status, tone: "neutral" };
}

const REASONS: Record<string, string> = {
  no_evidence: "nothing retrieved",
  model_insufficient: "the model said the excerpts weren't enough",
  no_valid_citation: "the answer cited no excerpt, so it was withheld",
};

export function abstainReasonLabel(reason: string): string {
  return REASONS[reason] ?? reason;
}

export function outcome(result: Pick<EvaluationResult, "outcome" | "abstain_reason">): {
  label: string;
  tone: Tone;
} {
  const labels: Record<EvaluationOutcome, { label: string; tone: Tone }> = {
    answered: { label: "Answered", tone: "success" },
    abstained: { label: "Abstained", tone: "neutral" },
    provider_error: { label: "Provider error", tone: "danger" },
  };
  return result.outcome ? labels[result.outcome] : { label: "Not answered", tone: "neutral" };
}

/** Was the answer-or-abstain decision right? null for provider errors (no decision). */
export function decisionLabel(correct: boolean | null): { label: string; tone: Tone } | null {
  if (correct === null) return null;
  return correct ? { label: "Correct decision", tone: "success" } : { label: "Wrong decision", tone: "danger" };
}

/** Variant labels as recorded with the run, so an old run keeps its own names. */
export function variantDetails(run: Pick<EvaluationRun, "config">, key: string): VariantDetails {
  return (
    run.config.variant_details?.[key] ?? {
      label: key,
      mode: key === "vector" ? "vector" : "hybrid",
      experimental: !["vector", "hybrid"].includes(key),
      lexical_config: null,
      lexical_match: null,
    }
  );
}

export function variantKeys(run: Pick<EvaluationRun, "config" | "summary">): string[] {
  return run.config.variants ?? Object.keys(run.summary.retrieval ?? {});
}

const CATEGORY: Record<string, string> = {
  direct: "Direct",
  paraphrase: "Paraphrase",
  exact_term: "Exact term",
  negative_policy: "Documented “no”",
  multi_source: "Several documents",
  follow_up: "Follow-up",
  out_of_scope: "Out of scope",
  absent_detail: "Not documented",
};

export function categoryLabel(category: string): string {
  return CATEGORY[category] ?? category;
}
