// Test data shaped like the API's responses (backend/app/api/v1/evaluations.py). Used only by
// the component tests; never imported by the app.

import type {
  AnswerSummary,
  EvaluationOverview,
  EvaluationQuestion,
  EvaluationResult,
  EvaluationRunDetail,
  RetrievalSummary,
} from "@/lib/types";

export function retrievalSummary(overrides: Partial<RetrievalSummary> = {}): RetrievalSummary {
  return {
    questions: 20,
    errors: 0,
    hit_rate_at_5: 1,
    recall_at_5: 0.95,
    mrr: 0.9,
    chance_hit_rate_at_5: 0.78,
    no_lexical_candidates: { answerable: null, unanswerable: null },
    latency_ms: { count: 26, min: 3, median: 5, p95: 9, max: 12 },
    unanswerable_questions: 6,
    ...overrides,
  };
}

export function answerSummary(overrides: Partial<AnswerSummary> = {}): AnswerSummary {
  return {
    questions: 26,
    decided: 25,
    provider_errors: 1,
    confusion: {
      answerable: { answered: 18, abstained: 1, provider_error: 1 },
      unanswerable: { answered: 1, abstained: 5, provider_error: 0 },
    },
    abstention_accuracy: 0.92,
    citation_questions: 18,
    citation_precision: 0.97,
    citation_recall: 0.88,
    abstain_reasons: { model_insufficient: 5, no_valid_citation: 1 },
    latency_ms: { count: 25, min: 900, median: 1500, p95: 3200, max: 4000 },
    generation_latency_ms: { count: 25, min: 800, median: 1400, p95: 3100, max: 3900 },
    ...overrides,
  };
}

export const QUESTION: EvaluationQuestion = {
  id: "q-1",
  key: "q01",
  position: 1,
  category: "direct",
  question: "How many days do I have to return an unused item?",
  previous_question: null,
  answerable: true,
  expected_source_keys: ["returns-policy.md"],
  expected_answer: "Within 30 days of delivery.",
  evidence: [{ source_key: "returns-policy.md", quote: "Unused items can be returned within 30 days" }],
  absent_terms: [],
  notes: null,
};

export const UNANSWERABLE: EvaluationQuestion = {
  ...QUESTION,
  id: "u-1",
  key: "u01",
  position: 2,
  category: "out_of_scope",
  question: "Who won the 1998 football World Cup?",
  answerable: false,
  expected_source_keys: [],
  expected_answer: null,
  evidence: [],
  absent_terms: ["World Cup", "football"],
};

export function result(overrides: Partial<EvaluationResult> = {}): EvaluationResult {
  return {
    variant: "vector",
    retrieval_mode: "vector",
    retrieval_query: QUESTION.question,
    retrieved: [],
    retrieved_sources: [
      { rank: 1, source_key: "returns-policy.md", name: "returns-policy.md", relevant: true, chunk_ranks: [1, 3] },
      { rank: 2, source_key: null, name: "thesis_report.pdf", relevant: false, chunk_ranks: [2] },
    ],
    vector_candidates: 10,
    lexical_candidates: null,
    top_vector_distance: 0.21,
    hit_at_5: true,
    recall_at_5: 1,
    reciprocal_rank: 1,
    embedding_latency_ms: 350,
    retrieval_latency_ms: 6,
    answer_evaluated: false,
    outcome: null,
    abstain_reason: null,
    answer: null,
    citations: [],
    citation_precision: null,
    citation_recall: null,
    abstention_correct: null,
    answer_latency_ms: null,
    token_usage: {},
    details: {},
    error_stage: null,
    error_category: null,
    error_message: null,
    ...overrides,
  };
}

const VARIANT_DETAILS = {
  vector: { label: "Vector only", mode: "vector", experimental: false, lexical_config: null, lexical_match: null },
  hybrid: {
    label: "Hybrid: keyword AND, simple (production)",
    mode: "hybrid",
    experimental: false,
    lexical_config: "simple",
    lexical_match: "all",
  },
  hybrid_or: {
    label: "Hybrid: keyword OR, simple",
    mode: "hybrid",
    experimental: true,
    lexical_config: "simple",
    lexical_match: "any",
  },
} as const;

export function runDetail(overrides: Partial<EvaluationRunDetail> = {}): EvaluationRunDetail {
  return {
    id: "run-1",
    status: "succeeded",
    dataset_name: "northstar-demo",
    dataset_version: "1",
    dataset_sha256: "abc",
    questions_total: 2,
    questions_done: 2,
    answer_modes: ["hybrid"],
    error_message: null,
    created_at: "2026-11-04T12:00:00Z",
    started_at: "2026-11-04T12:00:01Z",
    finished_at: "2026-11-04T12:05:00Z",
    config: {
      variants: ["vector", "hybrid", "hybrid_or"],
      variant_details: VARIANT_DETAILS,
      chat_model: "gemini-3.5-flash-lite",
      embedding_model: "gemini-embedding-001",
      embedding_dimensions: 768,
      top_n: 5,
      candidates: 10,
      rrf_k: 60,
    },
    corpus: { total_chunks: 10, other_sources: [] },
    summary: {
      questions_processed: 2,
      embedding_latency_ms: { count: 2, min: 300, median: 350, p95: 400, max: 400 },
      nearest_vector_distance: { answerable: null, unanswerable: null },
      retrieval: {
        vector: retrievalSummary(),
        hybrid: retrievalSummary({ mrr: 0, no_lexical_candidates: { answerable: 12, unanswerable: 6 } }),
        hybrid_or: retrievalSummary({ mrr: null, hit_rate_at_5: null, recall_at_5: null }),
      },
      answers: { hybrid: answerSummary() },
      gemini_requests: { query_embeddings: 2, generation_requests: 2 },
    },
    questions: [
      {
        question: QUESTION,
        results: [
          result(),
          result({
            variant: "hybrid",
            retrieval_mode: "hybrid",
            lexical_candidates: 0,
            answer_evaluated: true,
            outcome: "answered",
            answer: "Unused items can be returned within 30 days [S1].",
            citations: [
              {
                id: "S1",
                rank: 1,
                chunk_id: "c1",
                chunk_index: 0,
                document_id: "d1",
                document_title: "Returns Policy",
                source_id: "s1",
                source_name: "returns-policy.md",
                filename: "returns-policy.md",
                heading: null,
                page_start: null,
                page_end: null,
                excerpt: "Unused items…",
                source_key: "returns-policy.md",
              },
            ],
            citation_precision: 1,
            citation_recall: 1,
            abstention_correct: true,
            answer_latency_ms: 1500,
            token_usage: { generation_latency_ms: 1200 },
          }),
        ],
      },
      {
        question: UNANSWERABLE,
        results: [
          result({ hit_at_5: null, recall_at_5: null, reciprocal_rank: null }),
          result({
            variant: "hybrid",
            retrieval_mode: "hybrid",
            hit_at_5: null,
            recall_at_5: null,
            reciprocal_rank: null,
            answer_evaluated: true,
            outcome: "provider_error",
            error_stage: "generation",
            error_category: "unavailable",
            error_message: "Gemini is temporarily unavailable.",
          }),
        ],
      },
    ],
    ...overrides,
  };
}

export function overview(overrides: Partial<EvaluationOverview["corpus"]> = {}): EvaluationOverview {
  return {
    dataset: {
      name: "northstar-demo",
      version: "1",
      sha256: "abc",
      description: "Questions about the Northstar demo documents.",
      questions: 26,
      answerable: 20,
      unanswerable: 6,
      source_keys: ["product-guide.md", "returns-policy.md"],
    },
    corpus: {
      ready: true,
      sources: [
        { key: "product-guide.md", status: "ready", chunks: 2 },
        { key: "returns-policy.md", status: "ready", chunks: 2 },
      ],
      other_sources: [],
      total_chunks: 4,
      indexing: 0,
      ...overrides,
    },
    limits: {
      query_embeddings_per_run: 26,
      generations_per_answer_mode: 26,
      runs_left: 3,
      query_embeddings_left: 500,
      generations_left: 120,
      question_interval_seconds: 6,
    },
    questions: [QUESTION, UNANSWERABLE],
  };
}
