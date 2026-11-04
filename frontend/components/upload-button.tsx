"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { useRef } from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { MAX_UPLOAD_MB } from "@/lib/config";
import { ACCEPTED_EXTENSIONS, uploadProblem } from "@/lib/format";
import { sourcesKey } from "@/lib/queries";
import type { UploadResult } from "@/lib/types";

export type UploadOutcome =
  | { kind: "uploaded"; result: UploadResult }
  | { kind: "rejected"; fileName: string; message: string }
  | { kind: "failed"; fileName: string; error: unknown };

/** Picks one file, checks it, and uploads it. The outcome is reported to the page. */
export function UploadButton({ onOutcome }: { onOutcome: (outcome: UploadOutcome | null) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();
  const upload = useMutation({
    mutationFn: (file: File) => api.sources.upload(file),
    onSuccess: (result) => onOutcome({ kind: "uploaded", result }),
    onError: (error, file) => onOutcome({ kind: "failed", fileName: file.name, error }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: sourcesKey }),
  });

  function onChange(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // choosing the same file again still fires a change
    if (!file) return;
    const problem = uploadProblem(file, MAX_UPLOAD_MB);
    if (problem) {
      onOutcome({ kind: "rejected", fileName: file.name, message: problem });
      return;
    }
    onOutcome(null);
    upload.mutate(file);
  }

  return (
    <>
      <input
        ref={input}
        type="file"
        accept={ACCEPTED_EXTENSIONS.join(",")}
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={onChange}
      />
      <Button onClick={() => input.current?.click()} disabled={upload.isPending}>
        <Upload />
        {upload.isPending ? "Uploading…" : "Upload document"}
      </Button>
    </>
  );
}
