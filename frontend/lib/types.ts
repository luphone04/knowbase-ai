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

// --- evaluation (M9, ADR 0014) -------------------------------------------------------------

export type AnswerMode = "vector" | "hybrid";
export type EvaluationRunStatus = "queued" | "running" | "succeeded" | "failed";
export type CorpusSourceStatus = "ready" | "missing" | "not_ready" | "content_differs";
export type EvaluationOutcome = "answered" | "abstained" | "provider_error";

export type EvaluationQuestion = {
  id: string;
  key: string;
  position: number;
  category: string;
  question: string;
  previous_question: string | null;
  answerable: boolean;
  expected_source_keys: string[];
  expected_answer: string | null;
  evidence: { source_key: string; quote: string }[];
  absent_terms: string[];
  notes: string | null;
};

export type EvaluationOverview = {
  dataset: {
    name: string;
    version: string;
    sha256: string;
    description: string;
    questions: number;
    answerable: number;
    unanswerable: number;
    source_keys: string[];
  };
  corpus: {
    ready: boolean;
    sources: { key: string; status: CorpusSourceStatus; chunks: number }[];
    other_sources: { name: string; chunks: number; status: string }[];
    total_chunks: number;
    indexing: number;
  };
  limits: {
    query_embeddings_per_run: number;
    generations_per_answer_mode: number;
    runs_left: number;
    query_embeddings_left: number;
    generations_left: number;
    question_interval_seconds: number;
  };
  questions: EvaluationQuestion[];
};

/** Count, min, median, p95 (nearest rank), and max in milliseconds; null when nothing was measured. */
export type LatencyStats = {
  count: number;
  min: number;
  median: number;
  p95: number;
  max: number;
} | null;

export type DistanceStats = { count: number; min: number; median: number; max: number } | null;

/** Metrics are null when not measured (never 0 in place of "not measured"). */
export type RetrievalSummary = {
  questions: number;
  errors: number;
  hit_rate_at_5: number | null;
  recall_at_5: number | null;
  mrr: number | null;
  chance_hit_rate_at_5: number | null;
  no_lexical_candidates: { answerable: number | null; unanswerable: number | null };
  latency_ms: LatencyStats;
  unanswerable_questions: number;
};

type ConfusionRow = { answered: number; abstained: number; provider_error: number };

export type AnswerSummary = {
  questions: number;
  decided: number;
  provider_errors: number;
  confusion: { answerable: ConfusionRow; unanswerable: ConfusionRow };
  abstention_accuracy: number | null;
  citation_questions: number;
  citation_precision: number | null;
  citation_recall: number | null;
  abstain_reasons: Record<string, number>;
  latency_ms: LatencyStats;
  generation_latency_ms: LatencyStats;
};

export type EvaluationSummary = {
  questions_processed: number;
  embedding_latency_ms: LatencyStats;
  nearest_vector_distance: { answerable: DistanceStats; unanswerable: DistanceStats };
  retrieval: Record<string, RetrievalSummary>;
  answers: Partial<Record<AnswerMode, AnswerSummary>>;
  gemini_requests: { query_embeddings?: number; generation_requests?: number };
};

export type VariantDetails = {
  label: string;
  mode: "vector" | "hybrid";
  experimental: boolean;
  lexical_config: string | null;
  lexical_match: string | null;
};

export type EvaluationRun = {
  id: string;
  status: EvaluationRunStatus;
  dataset_name: string;
  dataset_version: string;
  dataset_sha256: string;
  questions_total: number;
  questions_done: number;
  answer_modes: AnswerMode[];
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** Models, retrieval settings, and variant_details, as recorded when the run started. */
  config: Record<string, unknown> & {
    variants?: string[];
    variant_details?: Record<string, VariantDetails>;
  };
  corpus: Record<string, unknown> & {
    sources?: { key: string; status: string; chunks: number }[];
    other_sources?: { name: string; chunks: number; status: string }[];
    total_chunks?: number;
  };
  /** Empty until the first question is stored (the list shows the summary saved at the end). */
  summary: Partial<EvaluationSummary>;
};

export type RetrievedChunk = {
  rank: number;
  chunk_id: string;
  chunk_index: number;
  source_id: string;
  source_key: string | null;
  name: string;
  heading: string | null;
  score: number;
  vector_rank: number | null;
  vector_distance: number | null;
  lexical_rank: number | null;
  lexical_score: number | null;
};

export type RetrievedSource = {
  rank: number;
  source_key: string | null;
  name: string;
  relevant: boolean;
  chunk_ranks: number[];
};

export type EvaluationResult = {
  variant: string;
  retrieval_mode: "vector" | "hybrid";
  retrieval_query: string;
  retrieved: RetrievedChunk[];
  retrieved_sources: RetrievedSource[];
  vector_candidates: number | null;
  lexical_candidates: number | null;
  top_vector_distance: number | null;
  hit_at_5: boolean | null;
  recall_at_5: number | null;
  reciprocal_rank: number | null;
  embedding_latency_ms: number | null;
  retrieval_latency_ms: number | null;
  answer_evaluated: boolean;
  outcome: EvaluationOutcome | null;
  abstain_reason: AbstainReason | null;
  answer: string | null;
  citations: (Citation & { source_key: string | null })[];
  citation_precision: number | null;
  citation_recall: number | null;
  abstention_correct: boolean | null;
  answer_latency_ms: number | null;
  token_usage: Record<string, unknown>;
  details: { model_text?: string; invalid_citation_ids?: string[]; excerpts?: number };
  error_stage: "embedding" | "generation" | null;
  error_category: string | null;
  error_message: string | null;
};

export type EvaluationRunDetail = EvaluationRun & {
  questions: { question: EvaluationQuestion; results: EvaluationResult[] }[];
};
