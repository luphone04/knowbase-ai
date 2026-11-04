"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, RefreshCw, Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { ErrorAlert } from "@/components/error-alert";
import { PageHeader } from "@/components/page-header";
import { PageLoading } from "@/components/page-loading";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ApiError, api } from "@/lib/api";
import { fileTypeLabel, formatBytes, formatDateTime, pageCount } from "@/lib/format";
import { sourceKey, sourcesKey, useSource } from "@/lib/queries";
import { useSession } from "@/lib/session";
import { isJobActive, isSourceActive, jobStatus, jobTypeLabel, sourceStatus } from "@/lib/status";
import type { Job, SourceDetail } from "@/lib/types";

const MANAGER_ROLES = ["owner", "admin"]; // the API allows only these to delete

export default function SourceDetailPage() {
  const { id } = useParams<{ id: string }>();
  const source = useSource(id);

  if (source.isPending) return <PageLoading label="Loading document…" />;
  if (source.isError) {
    const missing = source.error instanceof ApiError && source.error.status === 404;
    return (
      <>
        <PageHeader title={missing ? "Document not found" : "Document"} />
        <div className="grid max-w-xl gap-4 p-4 md:p-6">
          {missing ? (
            <p className="text-sm text-muted-foreground">
              This document doesn&apos;t exist in your workspace, or it has been deleted.
            </p>
          ) : (
            <ErrorAlert error={source.error} title="Couldn't load the document" />
          )}
          <BackLink />
        </div>
      </>
    );
  }
  return <SourceDetailView source={source.data} />;
}

function BackLink() {
  return (
    <Button variant="outline" size="sm" className="w-fit" asChild>
      <Link href="/sources">
        <ArrowLeft />
        All documents
      </Link>
    </Button>
  );
}

function SourceDetailView({ source }: { source: SourceDetail }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { data: session } = useSession();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const canDelete = !!session && MANAGER_ROLES.includes(session.workspace.role);
  const busy = isSourceActive(source) || source.jobs.some(isJobActive);

  const reindex = useMutation({
    mutationFn: () => api.sources.reindex(source.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: sourceKey(source.id) }),
  });
  const remove = useMutation({
    mutationFn: () => api.sources.remove(source.id),
    onSuccess: () => {
      // The list shows "Deleting" until the worker's delete job removes the source.
      void queryClient.invalidateQueries({ queryKey: sourcesKey });
      router.push("/sources");
    },
  });
  const status = sourceStatus(source.status);
  const pages = source.documents.map((d) => pageCount(d.metadata)).find((n) => n !== null) ?? null;

  return (
    <>
      <PageHeader
        title={source.display_name}
        description={<StatusBadge {...status} />}
        actions={
          <>
            <Button
              variant="outline"
              onClick={() => reindex.mutate()}
              disabled={busy || reindex.isPending}
            >
              <RefreshCw />
              {reindex.isPending ? "Queuing…" : "Re-index"}
            </Button>
            {canDelete ? (
              <Button
                variant="destructive"
                onClick={() => setConfirmDelete(true)}
                disabled={source.status === "deleting"}
              >
                <Trash2 />
                Delete
              </Button>
            ) : null}
          </>
        }
      />
      <div className="grid gap-6 p-4 md:p-6">
        <BackLink />
        <div aria-live="polite" className="grid gap-4 empty:hidden">
          {reindex.isError ? <ErrorAlert error={reindex.error} title="Couldn't re-index" /> : null}
          {remove.isError ? <ErrorAlert error={remove.error} title="Couldn't delete" /> : null}
          {source.status === "failed" && source.error_message ? (
            <ErrorAlert error={null} title="Indexing failed">
              <p>{source.error_message}</p>
            </ErrorAlert>
          ) : null}
        </div>

        <section aria-labelledby="details-heading" className="grid gap-3">
          <h2 id="details-heading" className="text-sm font-semibold">
            Details
          </h2>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3 lg:grid-cols-4">
            <Detail label="Type" value={fileTypeLabel(source.file_type)} />
            <Detail label="Size" value={formatBytes(source.file_size)} />
            {pages !== null ? <Detail label="Pages" value={String(pages)} /> : null}
            <Detail label="Chunks" value={String(source.chunk_count)} />
            <Detail label="Last indexed" value={formatDateTime(source.last_indexed_at)} />
            <Detail label="Uploaded" value={formatDateTime(source.created_at)} />
          </dl>
        </section>

        <section aria-labelledby="documents-heading" className="grid gap-3">
          <h2 id="documents-heading" className="text-sm font-semibold">
            Indexed content
          </h2>
          {source.documents.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Nothing is indexed yet{busy ? "; the worker is on it." : "."}
            </p>
          ) : (
            <ul className="grid gap-2 text-sm">
              {source.documents.map((document) => (
                <li key={document.id} className="rounded-lg border px-3 py-2">
                  <p className="font-medium">{document.title}</p>
                  <p className="text-muted-foreground">
                    {document.chunk_count} chunks
                    {pageCount(document.metadata) !== null
                      ? ` · ${pageCount(document.metadata)} pages`
                      : ""}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-labelledby="jobs-heading" className="grid gap-3">
          <h2 id="jobs-heading" className="text-sm font-semibold">
            Recent jobs
          </h2>
          <div className="rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Job</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="hidden sm:table-cell">Started</TableHead>
                  <TableHead className="hidden md:table-cell">Finished</TableHead>
                  <TableHead>Result</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {source.jobs.map((job) => (
                  <TableRow key={job.id} className="align-top">
                    <TableCell>{jobTypeLabel(job.job_type)}</TableCell>
                    <TableCell>
                      <StatusBadge {...jobStatus(job)} />
                    </TableCell>
                    <TableCell className="hidden text-muted-foreground sm:table-cell">
                      {formatDateTime(job.started_at)}
                    </TableCell>
                    <TableCell className="hidden text-muted-foreground md:table-cell">
                      {formatDateTime(job.finished_at)}
                    </TableCell>
                    <TableCell className="max-w-72 whitespace-normal">
                      {job.last_error ? (
                        <span className="text-destructive">{job.last_error}</span>
                      ) : (
                        <span className="text-muted-foreground">{jobResult(job)}</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </section>
      </div>

      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {source.display_name}?</DialogTitle>
            <DialogDescription>
              This removes the file, its indexed chunks, and its job history. Answers that cited it
              keep their saved excerpts. This can&apos;t be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">Cancel</Button>
            </DialogClose>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => {
                setConfirmDelete(false);
                remove.mutate();
              }}
            >
              {remove.isPending ? "Deleting…" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium tabular-nums">{value}</dd>
    </div>
  );
}

/** The worker's counts for a finished job, e.g. "Indexed 8 chunks (8 embedded, 0 reused)". */
function jobResult(job: Job): string {
  if (job.status !== "succeeded") return "—";
  const r = job.result;
  const n = (key: string) => (typeof r[key] === "number" ? (r[key] as number) : null);
  if (r.outcome === "unchanged") return `Unchanged: ${n("chunks") ?? 0} chunks kept, nothing embedded`;
  if (r.outcome === "indexed") {
    const parts = [`${n("chunks") ?? 0} chunks`];
    if (n("pages") !== null) parts.push(`${n("pages")} pages`);
    return `Indexed ${parts.join(", ")} (${n("embedded_texts") ?? 0} embedded, ${n("reused_embeddings") ?? 0} reused)`;
  }
  return "Done";
}
