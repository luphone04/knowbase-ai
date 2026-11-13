"use client";

import Link from "next/link";

import { ErrorAlert } from "@/components/error-alert";
import { DatasetPanel, StartRun } from "@/components/evaluation/start-run";
import { Score } from "@/components/evaluation/score";
import { PageHeader } from "@/components/page-header";
import { PageLoading } from "@/components/page-loading";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { isRunActive, providerErrorCount, runStatus } from "@/lib/evaluation";
import { formatDateTime } from "@/lib/format";
import { useEvaluationOverview, useEvaluationRuns } from "@/lib/queries";
import type { EvaluationRun } from "@/lib/types";

export default function EvaluationsPage() {
  const overview = useEvaluationOverview();
  const runs = useEvaluationRuns();
  const active = runs.data?.find(isRunActive);

  return (
    <>
      <PageHeader
        title="Evaluation"
        description="Measures retrieval, citations, and abstention on a fixed set of questions about this workspace's evaluation documents."
      />
      <div className="grid gap-6 p-4 md:p-6">
        {overview.isPending ? (
          <PageLoading label="Loading the dataset…" />
        ) : overview.isError ? (
          <ErrorAlert error={overview.error} title="Couldn't load the evaluation dataset" />
        ) : (
          <div className="grid gap-6 xl:grid-cols-2">
            <DatasetPanel overview={overview.data} />
            <StartRun overview={overview.data} activeRunId={active?.id} />
          </div>
        )}
        <RunList runs={runs} />
      </div>
    </>
  );
}

function RunList({ runs }: { runs: ReturnType<typeof useEvaluationRuns> }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>Runs</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {runs.isPending ? (
          <PageLoading label="Loading runs…" />
        ) : runs.isError ? (
          <ErrorAlert error={runs.error} title="Couldn't load the runs" />
        ) : runs.data.length === 0 ? (
          <p className="text-sm text-muted-foreground">No evaluation has been run in this workspace yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Started</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Questions</TableHead>
                  <TableHead>Answers</TableHead>
                  <TableHead className="text-right">MRR vector</TableHead>
                  <TableHead className="text-right">MRR hybrid</TableHead>
                  <TableHead className="text-right">Abstention accuracy (hybrid)</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.data.map((run) => (
                  <RunRow key={run.id} run={run} />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function RunRow({ run }: { run: EvaluationRun }) {
  const errors = providerErrorCount(run);
  return (
    <TableRow>
      <TableCell>
        <Link href={`/evaluations/${run.id}`} className="font-medium underline-offset-4 hover:underline">
          {formatDateTime(run.created_at)}
        </Link>
      </TableCell>
      <TableCell>
        <span className="flex flex-wrap items-center gap-1.5">
          <StatusBadge {...runStatus(run)} />
          {errors ? <span className="text-xs text-red-700">{errors} provider error(s)</span> : null}
        </span>
      </TableCell>
      <TableCell className="tabular-nums">
        {run.questions_done} of {run.questions_total}
      </TableCell>
      <TableCell>{run.answer_modes.length ? run.answer_modes.join(", ") : "retrieval only"}</TableCell>
      <TableCell className="text-right">
        <Score value={run.summary.retrieval?.vector?.mrr} />
      </TableCell>
      <TableCell className="text-right">
        <Score value={run.summary.retrieval?.hybrid?.mrr} />
      </TableCell>
      <TableCell className="text-right">
        <Score value={run.summary.answers?.hybrid?.abstention_accuracy} />
      </TableCell>
    </TableRow>
  );
}
