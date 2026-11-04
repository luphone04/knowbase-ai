// API types. They mirror backend/app/api/v1/schemas.py and backend/app/api/v1/ai.py;
// change them together. UUIDs and datetimes arrive as strings.

export type ApiErrorBody = {
  error: {
    code: string;
    message: string;
    details?: { loc: (string | number)[]; msg: string; type: string }[];
  };
};

// --- auth and workspaces ---------------------------------------------------------------

export type User = { id: string; email: string; created_at: string };

export type Workspace = { id: string; name: string; role: string; created_at: string };

export type Session = { user: User; workspace: Workspace };

export type RegisterInput = { email: string; password: string; workspace_name: string };

export type LoginInput = { email: string; password: string };

// --- sources and jobs -------------------------------------------------------------------

export type SourceStatus = "pending" | "processing" | "ready" | "failed" | "deleting";
export type JobStatus = "queued" | "running" | "succeeded" | "failed";
export type JobType = "ingest" | "reindex" | "delete";
export type FileType = "pdf" | "docx" | "txt" | "md";

export type JobSummary = {
  id: string;
  job_type: JobType;
  status: JobStatus;
  last_error: string | null;
};

/** `result` holds the worker's counts once a job succeeds (chunks, pages, embedded_texts, ...). */
export type Job = JobSummary & {
  source_id: string;
  attempts: number;
  max_attempts: number;
  next_run_at: string;
  result: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type Source = {
  id: string;
  kind: "file";
  display_name: string;
  original_filename: string | null;
  file_type: FileType | null;
  file_size: number | null;
  status: SourceStatus;
  error_message: string | null;
  last_indexed_at: string | null;
  created_at: string;
  updated_at: string;
  document_count: number;
  chunk_count: number;
  latest_job: JobSummary | null;
};

/** `metadata.page_count` is set for PDFs. */
export type SourceDocument = {
  id: string;
  title: string;
  content_hash: string;
  chunk_count: number;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type SourceDetail = Source & { documents: SourceDocument[]; jobs: Job[] };

/** 202 with a job, or 200 with `unchanged: true` (same bytes on a ready source). */
export type UploadResult = { source: Source; job: Job | null; unchanged: boolean };

// --- conversations and chat -------------------------------------------------------------

export type Citation = {
  id: string; // "S1", as used in the answer text
  rank: number;
  chunk_id: string;
  chunk_index: number;
  document_id: string;
  document_title: string;
  source_id: string;
  source_name: string;
  filename: string | null;
  heading: string | null;
  page_start: number | null;
  page_end: number | null;
  excerpt: string;
};

export type AbstainReason = "no_evidence" | "model_insufficient" | "no_valid_citation";

export type Message = {
  id: string;
  position: number;
  role: "user" | "assistant";
  content: string;
  outcome: "answered" | "abstained" | null; // assistant only
  abstain_reason: AbstainReason | null;
  citations: Citation[];
  token_usage: Record<string, unknown>;
  latency_ms: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type Conversation = { id: string; title: string; created_at: string; updated_at: string };

export type ConversationDetail = Conversation & { messages: Message[] };

export type ChatTurn = {
  conversation: Conversation;
  user_message: Message;
  assistant_message: Message;
  retrieval: {
    mode: string;
    retrieval_query: string;
    contextualized: boolean;
    trace_id: string;
    results: number;
  };
};

// --- AI status --------------------------------------------------------------------------

export type AIStatus = {
  provider: "gemini";
  status: "configured" | "missing_key" | "quota_error";
  configured: boolean;
  message: string;
  chat_model: string;
  thinking_level: string;
  max_output_tokens: number;
  embedding_model: string;
  embedding_dimensions: number;
  embedding_batch_size: number;
  document_task_type: string;
  query_task_type: string;
  max_retries: number;
  generation_max_retries: number;
  paid_fallback: false;
  last_error: { category: string; at: string } | null;
  last_success_at: string | null;
};
