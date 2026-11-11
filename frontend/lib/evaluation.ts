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
  ModelRates,
  UsageGroup,
  UsageIssue,
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

// --- token usage and estimated cost ----------------------------------------------------------

export const UNAVAILABLE = "Unavailable";

const tokenNumber = new Intl.NumberFormat("en-US");

/** "12,345", or null when nothing was reported (never shown as 0). */
export function formatTokens(value: number | null | undefined): string | null {
  return typeof value === "number" && Number.isFinite(value) ? tokenNumber.format(value) : null;
}

/**
 * USD with enough decimals for small amounts: $1.50, $0.0123, $0.000468, $0.00. null when the
 * cost couldn't be estimated.
 */
export function formatUsd(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  if (value === 0) return "$0.00";
  const decimals = Math.min(10, Math.max(2, Math.ceil(-Math.log10(value)) + 2));
  return `$${value.toFixed(decimals)}`;
}

/**
 * The estimated cost of a group of calls, as shown: unavailable, exact, or a lower bound when
 * some calls reported no usage.
 */
export function usageCost(group: Pick<UsageGroup, "cost_usd" | "usage_complete"> | undefined): {
  text: string;
  incomplete: boolean;
} {
  const usd = formatUsd(group?.cost_usd);
  if (!group || usd === null) return { text: UNAVAILABLE, incomplete: false };
  return group.usage_complete ? { text: usd, incomplete: false } : { text: `≥ ${usd}`, incomplete: true };
}

/** "$0.30 input · $2.50 output (incl. thinking) · $0.03 cached input" per 1M tokens. */
export function ratesLabel(rates: ModelRates | null | undefined): string {
  if (!rates) return "No price recorded";
  const parts = [`$${rates.input_per_million} input`];
  if (rates.output_per_million !== null) parts.push(`$${rates.output_per_million} output (incl. thinking)`);
  if (rates.cached_input_per_million !== null) parts.push(`$${rates.cached_input_per_million} cached input`);
  return parts.join(" · ");
}

const KIND_LABEL = { embedding: "query embedding", generation: "answer generation" } as const;

/** " (+2 retried attempts, not measured)", or "" when every request took one attempt. */
export function unmeasuredAttemptsNote(attempts: number | undefined): string {
  return attempts ? ` (+${attempts} retried attempt${attempts === 1 ? "" : "s"}, not measured)` : "";
}

/** Why an estimate is incomplete or unavailable, in a sentence. */
export function usageIssueText(issue: UsageIssue): string {
  switch (issue.code) {
    case "pricing_not_recorded":
      return "This run started before prices were recorded with each run, so it has no cost estimate. Token counts are shown where Gemini reported them.";
    case "model_unpriced":
      return `No verified price for ${issue.model} (${KIND_LABEL[issue.kind]}): its cost is unavailable rather than guessed.`;
    case "usage_missing":
      if (issue.kind === "embedding")
        return `${issue.requests} query embedding request(s) have no token counts: Gemini doesn't report token usage for embeddings. They are counted as requests but have no estimated cost, so the totals are a lower bound.`;
      return `${issue.requests} ${KIND_LABEL[issue.kind]} request(s) returned no token counts (for example, a failed call). They are left out, so the totals are a lower bound.`;
    case "unmeasured_attempts":
      return `${issue.attempts} failed ${KIND_LABEL[issue.kind]} attempt(s) were retried before the request succeeded. Gemini reports no token counts for failed attempts, and whether they are billed isn't known, so they are not included: the totals are a lower bound.`;
  }
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
