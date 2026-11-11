"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { ErrorAlert } from "@/components/error-alert";
import { RunDetail } from "@/components/evaluation/run-detail";
import { PageHeader } from "@/components/page-header";
import { PageLoading } from "@/components/page-loading";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useEvaluationRun } from "@/lib/queries";

export default function EvaluationRunPage() {
  const { id } = useParams<{ id: string }>();
  const run = useEvaluationRun(id);

  if (run.isPending) return <PageLoading label="Loading the evaluation run…" />;
  if (run.isError) {
    const missing = run.error instanceof ApiError && run.error.status === 404;
    return (
      <>
        <PageHeader title={missing ? "Evaluation run not found" : "Evaluation run"} />
        <div className="grid max-w-xl gap-4 p-4 md:p-6">
          {missing ? (
            <p className="text-sm text-muted-foreground">This run doesn&apos;t exist in your workspace.</p>
          ) : (
            <ErrorAlert error={run.error} title="Couldn't load the evaluation run" />
          )}
          <BackLink />
        </div>
      </>
    );
  }
  return (
    <>
      <PageHeader
        title="Evaluation run"
        description={`Started ${formatDateTime(run.data.created_at)}`}
        actions={<BackLink />}
      />
      <RunDetail run={run.data} />
    </>
  );
}

function BackLink() {
  return (
    <Button variant="outline" size="sm" className="w-fit" asChild>
      <Link href="/evaluations">
        <ArrowLeft />
        All runs
      </Link>
    </Button>
  );
}
