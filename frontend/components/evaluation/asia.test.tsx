// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QuestionResults } from "./question-results";
import { RetrievalComparison } from "./run-summary";
import { DatasetPanel, StartRun } from "./start-run";
import { overview, runDetail } from "./test-fixtures";
import type { EvidenceMapping } from "@/lib/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

afterEach(cleanup);

const FILES = {
  cobc: "ACC_Code of Business Conduct 2024_0.pdf",
  acp: "Anti-Corruption Policy.pdf",
};

function asiaOverview() {
  const base = overview();
  return {
    ...base,
    dataset: {
      ...base.dataset,
      id: "asia-cement-realworld",
      name: "asia-cement-realworld",
      version: "1.0.0",
      questions: 16,
      answerable: 13,
      unanswerable: 3,
      source_keys: ["acp", "cobc"],
      source_files: FILES,
      relevance: "passage" as const,
      split: "dev",
      held_out_questions: 8,
      evidence_mapping: {
        quotations: 28,
        matched: 28,
        matched_ignoring_spacing: 1,
        questions: 13,
        questions_fully_mapped: 13,
        problems: [] as EvidenceMapping["problems"],
      },
    },
    corpus: {
      ...base.corpus,
      ready: false,
      sources: [
        { key: "acp", status: "missing" as const, chunks: 0 },
        { key: "cobc", status: "ready" as const, chunks: 28 },
      ],
    },
  };
}

describe("Asia Cement run estimate", () => {
  it("shows the API's reservation: 18 query embeddings, 0 generations when retrieval only", () => {
    const data = asiaOverview();
    data.corpus = { ...data.corpus, ready: true };
    data.limits = { ...data.limits, query_embeddings_per_run: 18, generations_per_answer_mode: 16 };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <StartRun overview={data} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByLabelText(/Retrieval only/));
    expect(screen.getByTestId("estimate").textContent).toContain(
      "uses up to 18 query embeddings and 0 answer generations",
    );
    fireEvent.click(screen.getByLabelText(/Retrieval and hybrid answers/));
    expect(screen.getByTestId("estimate").textContent).toContain("18 query embeddings and 16 answer");
  });
});

describe("Asia Cement dataset panel", () => {
  it("shows the run split, the held-out count, file names, and the evidence mapping", () => {
    render(<DatasetPanel overview={asiaOverview()} />);
    expect(screen.getByText("Questions per run (dev split)").nextSibling?.textContent).toBe("16");
    expect(screen.getByText("Held out (never run automatically)").nextSibling?.textContent).toBe("8");
    expect(screen.getByText("Evidence passage")).toBeTruthy();
    expect(screen.getByText("Benchmark documents in this workspace")).toBeTruthy();
    expect(screen.getByText("Anti-Corruption Policy.pdf")).toBeTruthy();
    expect(screen.getByTestId("evidence-mapping").textContent).toMatch(
      /found in this workspace's chunks: 28 of 28 \(1 only when PDF spacing is ignored\)/,
    );
    expect(screen.getByTestId("corpus-help").textContent).toMatch(/Upload the 2 benchmark files above/);
    expect(screen.queryByText(/sample_data\/demo_store/)).toBeNull();
  });

  it("names the passages that couldn't be mapped", () => {
    const data = asiaOverview();
    data.dataset.evidence_mapping = {
      ...data.dataset.evidence_mapping,
      matched: 27,
      problems: [{ question: "AC-001", evidence: 1, status: "split", detail: "spans chunks 3 and 4" }],
    };
    render(<DatasetPanel overview={data} />);
    expect(screen.getByTestId("evidence-mapping").textContent).toMatch(/Not mapped, and not scored: AC-001 \(split\)/);
  });

  it("keeps the Northstar panel as it was", () => {
    render(<DatasetPanel overview={overview()} />);
    expect(screen.getByText("Demo documents in this workspace")).toBeTruthy();
    expect(screen.queryByText(/Held out/)).toBeNull();
    expect(screen.queryByTestId("evidence-mapping")).toBeNull();
  });
});

function passageRun() {
  const run = runDetail();
  run.config = { ...run.config, relevance: "passage", question_split: "dev" };
  run.summary = {
    ...run.summary,
    relevance: "passage",
    document_retrieval: {
      vector: { questions: 13, hit_rate_at_5: 1, recall_at_5: 1, mrr: 0.95 },
      hybrid: { questions: 13, hit_rate_at_5: 1, recall_at_5: 1, mrr: 0.9 },
      hybrid_or: { questions: 13, hit_rate_at_5: 1, recall_at_5: 1, mrr: 0.9 },
    },
    evidence_mapping: {
      quotations: 28,
      matched: 28,
      matched_ignoring_spacing: 0,
      questions: 13,
      questions_fully_mapped: 13,
      problems: [],
    },
  };
  return run;
}

describe("passage-level results", () => {
  it("labels the metrics as evidence passages and lists document-level results apart", () => {
    render(<RetrievalComparison run={passageRun()} />);
    expect(screen.getByText(/^MRR \(evidence passage\):/)).toBeTruthy();
    expect(screen.getByText(/the right document with the wrong chunk is a miss/)).toBeTruthy();
    expect(screen.getByTestId("document-retrieval").textContent).toMatch(
      /Right document in the top 5 \(document level, not evidence\): Vector only HitRate@5 1\.00, MRR 0\.95/,
    );
    expect(screen.getByTestId("run-evidence-mapping").textContent).toMatch(/28 of 28/);
  });

  it("keeps source-level wording for Northstar runs", () => {
    render(<RetrievalComparison run={runDetail()} />);
    expect(screen.getByText(/Source level: the top 5 chunks are collapsed/)).toBeTruthy();
    expect(screen.queryByTestId("document-retrieval")).toBeNull();
  });

  it("shows which retrieved chunk holds the evidence, or that none did", () => {
    const run = passageRun();
    const [first] = run.questions;
    first.results = first.results.map((r) =>
      r.variant === "vector"
        ? {
            ...r,
            retrieved: [
              { ...r.retrieved[0], rank: 1, evidence: false },
              { ...r.retrieved[0], rank: 2, chunk_id: "c2", evidence: true },
            ],
          }
        : { ...r, hit_at_5: false, retrieved: r.retrieved.map((c) => ({ ...c, evidence: false })) },
    );
    render(<QuestionResults run={run} />);
    const item = screen.getByTestId(`question-${first.question.key}`);
    expect(within(item).getByTestId("evidence-vector").textContent).toBe("Evidence passage in chunk rank 2");
    expect(within(item).getByTestId("evidence-hybrid").textContent).toBe("No chunk holding the evidence in the top 5");
  });
});
