"use client";

import { useQuery } from "@tanstack/react-query";
import { FileText, MessageSquare } from "lucide-react";
import Link from "next/link";

import { DailyAllowance } from "@/components/daily-allowance";
import { ErrorAlert } from "@/components/error-alert";
import { PageHeader } from "@/components/page-header";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useSources } from "@/lib/queries";
import { useSession } from "@/lib/session";
import type { Tone } from "@/lib/status";
import { sourceStatus } from "@/lib/status";
import type { AIStatus, Source } from "@/lib/types";

export default function DashboardPage() {
  const { data: session } = useSession();
  if (!session) return null; // the layout renders pages only with a session

  return (
    <>
      <PageHeader
        title={session.workspace.name}
        description={`Signed in as ${session.user.email} (${session.workspace.role})`}
        actions={
          <>
            <Button variant="outline" asChild>
              <Link href="/sources">
                <FileText />
                Documents
              </Link>
            </Button>
            <Button asChild>
              <Link href="/chat">
                <MessageSquare />
                Ask a question
              </Link>
            </Button>
          </>
        }
      />
      <div className="grid gap-6 p-4 md:p-6">
        <DocumentSummary />
        <DailyAllowances />
        <AIStatusCard />
      </div>
    </>
  );
}

/** What's left of today's chat questions. */
function DailyAllowances() {
  return (
    <section aria-labelledby="daily-allowances" className="grid gap-1">
      <h2 id="daily-allowances" className="text-sm font-semibold">
        Today&apos;s allowance
      </h2>
      <DailyAllowance />
    </section>
  );
}

/** Totals over GET /sources, which returns every source in the workspace. */
function DocumentSummary() {
  const sources = useSources();
  if (sources.isPending) {
    return (
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5" role="status" aria-label="Loading documents">
        {Array.from({ length: 5 }, (_, i) => (
          <Skeleton key={i} className="h-20" />
        ))}
      </div>
    );
  }
  if (sources.isError) return <ErrorAlert error={sources.error} title="Couldn't load documents" />;

  const all = sources.data;
  const count = (...statuses: Source["status"][]) => all.filter((s) => statuses.includes(s.status)).length;
  const chunks = all.reduce((sum, s) => sum + s.chunk_count, 0);
  const recent = [...all].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 5);

  return (
    <section aria-labelledby="documents-summary" className="grid gap-3">
      <h2 id="documents-summary" className="text-sm font-semibold">
        Documents
      </h2>
      <dl className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Stat label="Documents" value={all.length} />
        <Stat label="Ready" value={count("ready")} />
        <Stat label="Queued or processing" value={count("pending", "processing")} />
        <Stat label="Failed" value={count("failed")} />
        <Stat label="Indexed chunks" value={chunks} />
      </dl>
      {all.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No documents yet.{" "}
          <Link href="/sources" className="font-medium text-foreground underline underline-offset-4">
            Upload one
          </Link>{" "}
          to start asking questions.
        </p>
      ) : (
        <ul className="divide-y rounded-lg border text-sm">
          {recent.map((source) => (
            <li key={source.id} className="flex items-center justify-between gap-3 px-3 py-2">
              <Link
                href={`/sources/${source.id}`}
                className="min-w-0 truncate font-medium underline-offset-4 hover:underline"
              >
                {source.display_name}
              </Link>
              <span className="flex shrink-0 items-center gap-3">
                <span className="hidden text-xs text-muted-foreground sm:inline">
                  {formatDateTime(source.updated_at)}
                </span>
                <StatusBadge {...sourceStatus(source.status)} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border px-3 py-2.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-2xl font-semibold tabular-nums">{value}</dd>
    </div>
  );
}

const AI_STATUS: Record<AIStatus["status"], { label: string; tone: Tone }> = {
  configured: { label: "Configured", tone: "success" },
  missing_key: { label: "Missing API key", tone: "danger" },
  quota_error: { label: "Quota reached", tone: "warning" },
};

function AIStatusCard() {
  // GET /ai/status reads the API's settings and last provider outcome; it never calls Gemini.
  const status = useQuery({ queryKey: ["ai-status"], queryFn: api.ai.status });
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>AI provider</h2>
        </CardTitle>
        <CardDescription>Gemini free tier, used for embeddings and answers. No paid fallback.</CardDescription>
      </CardHeader>
      <CardContent>
        {status.isPending ? (
          <Skeleton className="h-16" />
        ) : status.isError ? (
          <ErrorAlert error={status.error} title="Couldn't load the AI status" />
        ) : (
          <div className="grid gap-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge {...AI_STATUS[status.data.status]} />
              <span className="text-muted-foreground">{status.data.message}</span>
            </div>
            <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-4">
              <Info label="Answer model" value={status.data.chat_model} />
              <Info
                label="Embedding model"
                value={`${status.data.embedding_model} (${status.data.embedding_dimensions} dims)`}
              />
              <Info label="Last successful call" value={formatDateTime(status.data.last_success_at)} />
              <Info
                label="Last error"
                value={
                  status.data.last_error
                    ? `${status.data.last_error.category}, ${formatDateTime(status.data.last_error.at)}`
                    : "None"
                }
              />
            </dl>
            <p className="text-xs text-muted-foreground">
              Last call and error are what this API process has seen since it started.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="truncate font-medium" title={value}>
        {value}
      </dd>
    </div>
  );
}
