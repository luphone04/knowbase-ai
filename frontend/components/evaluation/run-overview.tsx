import { CircleAlert } from "lucide-react";

import { StatusBadge } from "@/components/status-badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { CollapsibleCard } from "@/components/collapsible-card";
import { isRunActive, providerErrorCount, runStatus } from "@/lib/evaluation";
import { formatDateTime } from "@/lib/format";
import type { EvaluationRunDetail } from "@/lib/types";

/** Status, progress, and what the run measured: dataset, models, settings, and corpus. */
export function RunOverview({ run, defaultOpen = true }: { run: EvaluationRunDetail; defaultOpen?: boolean }) {
  const answerable = run.questions.filter((q) => q.question.answerable).length;
  const config = run.config;
  const corpus = run.corpus;
  const requests = run.summary.gemini_requests;
  const retries = run.summary.retry_reservations;
  const errors = providerErrorCount(run);
  const percent = run.questions_total ? Math.round((run.questions_done / run.questions_total) * 100) : 0;

  return (
    <CollapsibleCard
      title="Overview"
      defaultOpen={defaultOpen}
      data-testid="section-overview"
      badges={
        <>
          <StatusBadge {...runStatus(run)} />
          {errors ? <StatusBadge label={`${errors} provider error(s)`} tone="warning" /> : null}
        </>
      }
      summary={`${run.questions_done} of ${run.questions_total} questions evaluated · ${run.dataset_name} v${run.dataset_version} · ${String(config.chat_model ?? "—")}`}
      contentClassName="grid gap-4 text-sm"
    >
      {isRunActive(run) ? (
        <div className="grid gap-1.5" role="status" aria-live="polite">
          <p>
            {run.status === "queued" ? "Waiting for the worker…" : "Evaluating"}{" "}
            {run.questions_done} of {run.questions_total} questions. This page refreshes on its
            own.
          </p>
          <div
            className="h-2 overflow-hidden rounded-full bg-muted"
            role="progressbar"
            aria-label="Questions evaluated"
            aria-valuemin={0}
            aria-valuemax={run.questions_total}
            aria-valuenow={run.questions_done}
          >
            <div className="h-full bg-foreground transition-all" style={{ width: `${percent}%` }} />
          </div>
        </div>
      ) : null}
      {run.status === "failed" ? (
        <Alert variant="destructive" role="alert">
          <CircleAlert />
          <AlertTitle>The run stopped after {run.questions_done} of {run.questions_total} questions</AlertTitle>
          <AlertDescription>
            <p>{run.error_message}</p>
            <p>The results below cover only the questions evaluated before it stopped.</p>
          </AlertDescription>
        </Alert>
      ) : null}
      <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
        <Info label="Dataset" value={`${run.dataset_name} v${run.dataset_version}`} />
        <Info
          label="Questions"
          value={`${run.questions_total} (${answerable} answerable, ${run.questions_total - answerable} unanswerable)`}
        />
        <Info label="Finished" value={run.finished_at ? formatDateTime(run.finished_at) : "—"} />
        <Info label="Answer model" value={String(config.chat_model ?? "—")} />
        <Info
          label="Embedding model"
          value={`${String(config.embedding_model ?? "—")} (${String(config.embedding_dimensions ?? "—")} dims)`}
        />
        <Info
          label="Retrieval"
          value={`top ${String(config.top_n)} of ${String(config.candidates)} candidates per list, RRF k = ${String(config.rrf_k)}`}
        />
        <Info
          label="Answers measured with"
          value={run.answer_modes.length ? run.answer_modes.join(" and ") : "none (retrieval only)"}
        />
        <Info
          label="Corpus"
          value={`${String(corpus.total_chunks ?? "—")} chunks; ${corpus.other_sources?.length ?? 0} document(s) outside the dataset`}
        />
        <Info
          label="Gemini requests (saved results)"
          value={`${requests?.query_embeddings ?? 0} query embeddings, ${requests?.generation_requests ?? 0} generations`}
        />
        {retries && retries.count > 0 ? (
          <Info
            label="Additional retry reservations"
            value={`${retries.count} retried question(s): ${retries.query_embeddings} query embeddings, ${retries.generations} generations reserved for interrupted attempts (an upper bound)`}
          />
        ) : null}
      </dl>
    </CollapsibleCard>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium break-words">{value}</dd>
    </div>
  );
}
