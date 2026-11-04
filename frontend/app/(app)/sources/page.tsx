"use client";

import { CircleCheck, FileText, Info } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ErrorAlert } from "@/components/error-alert";
import { PageHeader } from "@/components/page-header";
import { PageLoading } from "@/components/page-loading";
import { StatusBadge } from "@/components/status-badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { UploadButton, type UploadOutcome } from "@/components/upload-button";
import { MAX_UPLOAD_MB } from "@/lib/config";
import { fileTypeLabel, formatDateTime } from "@/lib/format";
import { useSources } from "@/lib/queries";
import { sourceStatus } from "@/lib/status";

export default function SourcesPage() {
  const sources = useSources();
  const [outcome, setOutcome] = useState<UploadOutcome | null>(null);

  return (
    <>
      <PageHeader
        title="Documents"
        description={`PDF, DOCX, TXT, or Markdown, up to ${MAX_UPLOAD_MB} MB each. Uploading a file with the same name replaces it.`}
        actions={<UploadButton onOutcome={setOutcome} />}
      />
      <div className="grid gap-4 p-4 md:p-6">
        <div aria-live="polite">{outcome ? <UploadNotice outcome={outcome} /> : null}</div>

        {sources.isPending ? (
          <PageLoading label="Loading documents…" />
        ) : sources.isError ? (
          <ErrorAlert error={sources.error} title="Couldn't load documents">
            <Button variant="outline" size="sm" className="mt-3" onClick={() => sources.refetch()}>
              Try again
            </Button>
          </ErrorAlert>
        ) : sources.data.length === 0 ? (
          <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed px-6 py-12 text-center">
            <FileText className="size-8 text-muted-foreground" aria-hidden />
            <p className="font-medium">No documents yet</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              Upload a document to index it. You can ask questions about it once it shows Ready.
            </p>
          </div>
        ) : (
          <div className="rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead className="hidden sm:table-cell">Type</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="hidden text-right sm:table-cell">Chunks</TableHead>
                  <TableHead className="hidden md:table-cell">Last indexed</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sources.data.map((source) => {
                  const status = sourceStatus(source.status);
                  return (
                    <TableRow key={source.id}>
                      <TableCell className="max-w-48 sm:max-w-80">
                        <Link
                          href={`/sources/${source.id}`}
                          className="block truncate font-medium underline-offset-4 hover:underline"
                          title={source.display_name}
                        >
                          {source.display_name}
                        </Link>
                        {source.status === "failed" && source.error_message ? (
                          <p className="truncate text-xs text-destructive" title={source.error_message}>
                            {source.error_message}
                          </p>
                        ) : null}
                      </TableCell>
                      <TableCell className="hidden sm:table-cell">
                        {fileTypeLabel(source.file_type)}
                      </TableCell>
                      <TableCell>
                        <StatusBadge {...status} />
                      </TableCell>
                      <TableCell className="hidden text-right tabular-nums sm:table-cell">
                        {source.chunk_count}
                      </TableCell>
                      <TableCell className="hidden text-muted-foreground md:table-cell">
                        {formatDateTime(source.last_indexed_at)}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
    </>
  );
}

function UploadNotice({ outcome }: { outcome: UploadOutcome }) {
  if (outcome.kind === "failed") {
    return <ErrorAlert error={outcome.error} title={`Upload failed: ${outcome.fileName}`} />;
  }
  if (outcome.kind === "rejected") {
    return (
      <Alert variant="destructive" role="alert">
        <Info />
        <AlertTitle>Can&apos;t upload {outcome.fileName}</AlertTitle>
        <AlertDescription>{outcome.message}</AlertDescription>
      </Alert>
    );
  }
  const { source, unchanged } = outcome.result;
  return (
    <Alert>
      <CircleCheck />
      <AlertTitle>{unchanged ? "Already indexed" : "Uploaded"}</AlertTitle>
      <AlertDescription>
        {unchanged
          ? `${source.display_name} hasn't changed since it was indexed, so nothing was done.`
          : `${source.display_name} is queued. Its status below updates as the worker indexes it.`}
      </AlertDescription>
    </Alert>
  );
}
