import type { FileType } from "./types";

export function formatBytes(bytes: number | null): string {
  if (bytes === null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatDateTime(value: string | null): string {
  return value ? dateTime.format(new Date(value)) : "—";
}

const FILE_TYPES: Record<FileType, string> = {
  pdf: "PDF",
  docx: "DOCX",
  txt: "Text",
  md: "Markdown",
};

export function fileTypeLabel(type: FileType | null): string {
  return type ? (FILE_TYPES[type] ?? type.toUpperCase()) : "—";
}

/** The page count the parser recorded for a PDF (documents[].metadata.page_count), if any. */
export function pageCount(metadata: Record<string, unknown>): number | null {
  const value = metadata.page_count;
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

export const ACCEPTED_EXTENSIONS = [".pdf", ".docx", ".txt", ".md"] as const;

/** A quick check before uploading; the server validates type, signature, and size again. */
export function uploadProblem(file: Pick<File, "name" | "size">, maxMb: number): string | null {
  const name = file.name.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext))) {
    return "Only PDF, DOCX, TXT, and Markdown (.md) files can be uploaded.";
  }
  if (file.size === 0) return "The file is empty.";
  if (file.size > maxMb * 1024 * 1024) return `The file is larger than the ${maxMb} MB limit.`;
  return null;
}
