"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ErrorAlert } from "@/components/error-alert";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { corpusStatus, requestEstimate } from "@/lib/evaluation";
import { evaluationsKey } from "@/lib/queries";
import type { AnswerMode, EvaluationOverview } from "@/lib/types";

/** The dataset, whether this workspace holds its documents, and what each check costs. */
export function DatasetPanel({ overview }: { overview: EvaluationOverview }) {
  const { dataset, corpus } = overview;
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>Dataset</h2>
        </CardTitle>
        <CardDescription>{dataset.description}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Fact label="Dataset" value={`${dataset.name} v${dataset.version}`} />
          <Fact label="Questions" value={String(dataset.questions)} />
          <Fact label="Answerable" value={String(dataset.answerable)} />
          <Fact label="Unanswerable" value={String(dataset.unanswerable)} />
        </dl>
        <section className="grid gap-2" aria-labelledby="corpus-heading">
          <h3 id="corpus-heading" className="font-semibold">
            Demo documents in this workspace
          </h3>
          <ul className="divide-y rounded-lg border">
            {corpus.sources.map((source) => (
              <li key={source.key} className="flex items-center justify-between gap-3 px-3 py-2">
                <span className="min-w-0 truncate font-mono text-xs">{source.key}</span>
                <span className="flex items-center gap-3">
                  {source.status === "ready" ? (
                    <span className="text-xs text-muted-foreground">{source.chunks} chunks</span>
                  ) : null}
                  <StatusBadge {...corpusStatus(source.status)} />
                </span>
              </li>
            ))}
          </ul>
          {corpus.ready ? null : (
            <p className="text-muted-foreground" data-testid="corpus-help">
              {corpus.indexing > 0
                ? `${corpus.indexing} document(s) are still being indexed. Wait until every document is ready.`
                : "Upload the five files from sample_data/demo_store, unchanged, on the "}
              {corpus.indexing > 0 ? null : (
                <>
                  <Link href="/sources" className="font-medium text-foreground underline underline-offset-4">
                    Documents
                  </Link>{" "}
                  page, and wait until each one is ready.
                </>
              )}
            </p>
          )}
          {corpus.other_sources.length ? (
            <p className="text-xs text-muted-foreground">
              {corpus.other_sources.length} other document(s) in this workspace (
              {corpus.other_sources.map((o) => o.name).join(", ")}) are searched too and compete with
              the demo documents. The run records them with its results.
            </p>
          ) : null}
        </section>
      </CardContent>
    </Card>
  );
}

const CHOICES: { modes: AnswerMode[]; label: string; detail: string }[] = [
  {
    modes: [],
    label: "Retrieval only",
    detail: "Every retrieval configuration; no answers.",
  },
  {
    modes: ["hybrid"],
    label: "Retrieval and hybrid answers",
    detail: "Answers with production hybrid retrieval.",
  },
  {
    modes: ["vector", "hybrid"],
    label: "Retrieval, vector and hybrid answers",
    detail: "Answers with both production modes.",
  },
];

export function StartRun({ overview, activeRunId }: { overview: EvaluationOverview; activeRunId?: string }) {
  const [choice, setChoice] = useState(1);
  const router = useRouter();
  const queryClient = useQueryClient();
  const start = useMutation({
    mutationFn: api.evaluations.start,
    onSuccess: async (run) => {
      await queryClient.invalidateQueries({ queryKey: evaluationsKey });
      router.push(`/evaluations/${run.id}`);
    },
  });
  const { limits, corpus } = overview;
  const modes = CHOICES[choice].modes;
  const estimate = requestEstimate(overview.dataset.questions, modes);
  const blocked = !corpus.ready || Boolean(activeRunId);

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>Start an evaluation</h2>
        </CardTitle>
        <CardDescription>
          Runs in the worker, one question at a time ({limits.question_interval_seconds} s apart),
          on Gemini&apos;s free tier. The requests are reserved from today&apos;s budget when the run
          starts; unused ones are given back.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="grid gap-4 text-sm"
          onSubmit={(event) => {
            event.preventDefault();
            start.mutate(modes);
          }}
        >
          <fieldset className="grid gap-2">
            <legend className="mb-1 font-semibold">What to measure</legend>
            {CHOICES.map((option, index) => (
              <label
                key={option.label}
                className="flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2 has-[:checked]:border-foreground"
              >
                <input
                  type="radio"
                  name="answer-modes"
                  className="mt-1"
                  checked={choice === index}
                  onChange={() => setChoice(index)}
                />
                <span>
                  <span className="font-medium">{option.label}</span>
                  <span className="block text-muted-foreground">{option.detail}</span>
                </span>
              </label>
            ))}
          </fieldset>
          <p data-testid="estimate">
            This run uses up to <strong>{estimate.queryEmbeddings}</strong> query embeddings and{" "}
            <strong>{estimate.generations}</strong> answer generations. Left today:{" "}
            {limits.query_embeddings_left} embeddings, {limits.generations_left} generations,{" "}
            {limits.runs_left} run(s) for this workspace.
          </p>
          {start.isError ? <ErrorAlert error={start.error} title="The evaluation didn't start" /> : null}
          {activeRunId ? (
            <p className="text-muted-foreground">
              An evaluation is already running.{" "}
              <Link href={`/evaluations/${activeRunId}`} className="font-medium text-foreground underline underline-offset-4">
                Follow its progress
              </Link>
              .
            </p>
          ) : null}
          <Button type="submit" className="w-fit" disabled={blocked || start.isPending}>
            <Play />
            {start.isPending ? "Starting…" : "Start evaluation"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border px-3 py-2">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="font-semibold tabular-nums">{value}</dd>
    </div>
  );
}
