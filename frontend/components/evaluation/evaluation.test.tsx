// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QuestionResults } from "./question-results";
import { RunOverview } from "./run-overview";
import { AnswerEvaluation, RetrievalComparison } from "./run-summary";
import { DatasetPanel, StartRun } from "./start-run";
import { answerSummary, overview, runDetail } from "./test-fixtures";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  push.mockReset();
});

function withQueryClient(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>;
}

describe("RetrievalComparison", () => {
  it("shows measured values, zero as zero, and missing values as not measured", () => {
    render(<RetrievalComparison run={runDetail()} />);
    const vector = screen.getByTestId("retrieval-vector");
    expect(within(vector).getByText("Vector only")).toBeTruthy();
    expect(within(vector).getByText("0.90")).toBeTruthy(); // MRR
    expect(within(vector).getByText("Production mode")).toBeTruthy();

    const hybrid = screen.getByTestId("retrieval-hybrid");
    expect(within(hybrid).getByText("0.00")).toBeTruthy(); // a measured zero stays 0.00
    expect(within(hybrid).getByText(/12 answerable/)).toBeTruthy();

    const experiment = screen.getByTestId("retrieval-hybrid_or");
    expect(within(experiment).getByText("Experiment")).toBeTruthy();
    expect(within(experiment).getAllByText("Not measured")).toHaveLength(3);
    expect(screen.getByText(/random ranking of this corpus would reach HitRate@5/)).toBeTruthy();
    expect(screen.getByText("0.78")).toBeTruthy();
  });
});

describe("AnswerEvaluation", () => {
  it("shows the decisions and separates provider errors", () => {
    render(<AnswerEvaluation run={runDetail()} />);
    const hybrid = screen.getByTestId("answers-hybrid");
    expect(within(hybrid).getByText("0.92")).toBeTruthy();
    expect(within(hybrid).getByText("(over 25 decided)")).toBeTruthy();
    const matrix = within(hybrid).getByRole("table", { name: "Hybrid decisions" });
    const answerable = within(matrix).getByText("Answerable").closest("tr")!;
    expect(within(answerable).getAllByRole("cell").map((c) => c.textContent)).toEqual(["18", "1", "1"]);
    expect(within(hybrid).getByText(/5 × the model said the excerpts weren't enough/)).toBeTruthy();
  });

  it("says a mode was not measured instead of showing zeros", () => {
    render(<AnswerEvaluation run={runDetail()} />);
    expect(within(screen.getByTestId("answers-vector")).getByText(/Not measured: this run didn't answer/)).toBeTruthy();
  });

  it("shows not measured when no answerable question was answered", () => {
    const run = runDetail();
    run.summary.answers = {
      hybrid: answerSummary({ citation_precision: null, citation_recall: null, citation_questions: 0 }),
    };
    render(<AnswerEvaluation run={run} />);
    expect(within(screen.getByTestId("answers-hybrid")).getAllByText("Not measured")).toHaveLength(2);
  });
});

describe("failed-attempt latency", () => {
  it("labels successful-answer latency and shows failed attempts separately", () => {
    render(<AnswerEvaluation run={runDetail()} />);
    const hybrid = screen.getByTestId("answers-hybrid");
    expect(within(hybrid).getByText("Answer latency (successful answers)")).toBeTruthy();
    expect(within(hybrid).getByText("median 1500 ms · p95 3200 ms (n = 25)")).toBeTruthy();
    expect(within(hybrid).getByText("Failed attempts, until the error")).toBeTruthy();
    expect(within(hybrid).getByText("median 90.5 s · p95 90.5 s (n = 1)")).toBeTruthy();
  });

  it("shows not measured for a run that recorded no failure timing", () => {
    const run = runDetail();
    run.summary.answers = { hybrid: answerSummary({ failed_attempt_latency_ms: undefined }) };
    render(<AnswerEvaluation run={run} />);
    const line = screen.getByText("Failed attempts, until the error").closest("div")!;
    expect(within(line).getByText("Not measured")).toBeTruthy();
  });

  it("has no failed-attempt line without provider errors", () => {
    const run = runDetail();
    run.summary.answers = { hybrid: answerSummary({ provider_errors: 0 }) };
    render(<AnswerEvaluation run={run} />);
    expect(screen.queryByText("Failed attempts, until the error")).toBeNull();
  });

  it("shows how long a failed question took, or that it wasn't recorded", () => {
    render(<QuestionResults run={runDetail()} />);
    expect(within(screen.getByTestId("question-u01")).getByText("Failed after 90.5 s")).toBeTruthy();
    cleanup();
    const old = runDetail();
    old.questions[1].results[1] = { ...old.questions[1].results[1], details: {} };
    render(<QuestionResults run={old} />);
    expect(
      within(screen.getByTestId("question-u01")).getByText(/not recorded for this run/),
    ).toBeTruthy();
  });
});

describe("QuestionResults", () => {
  it("lets a reviewer inspect expected documents, retrieval, answers, and errors", () => {
    render(<QuestionResults run={runDetail()} />);
    const q01 = screen.getByTestId("question-q01");
    fireEvent.click(q01.querySelector("summary")!);
    expect((q01 as HTMLDetailsElement).open).toBe(true);
    expect(within(q01).getByText("Within 30 days of delivery.")).toBeTruthy();
    const vector = within(q01).getByTestId("retrieved-vector");
    expect(within(vector).getByText(/returns-policy.md\s*\(expected\)/)).toBeTruthy();
    expect(within(vector).getByText(/thesis_report.pdf\s*\(not in the dataset\)/)).toBeTruthy();
    const answer = within(q01).getByTestId("answer-hybrid");
    expect(within(answer).getByText("Answered")).toBeTruthy();
    expect(within(answer).getByText("Correct decision")).toBeTruthy();
    expect(within(answer).getByText(/S1 returns-policy.md/)).toBeTruthy();

    const u01 = screen.getByTestId("question-u01");
    expect(within(u01).getByText("Unanswerable")).toBeTruthy();
    const failed = within(u01).getByTestId("answer-hybrid");
    expect(within(failed).getByText("Provider error")).toBeTruthy();
    expect(within(failed).queryByText("Correct decision")).toBeNull();
    expect(within(failed).queryByText("Abstained")).toBeNull();
    expect(within(failed).getByText(/unavailable: Gemini is temporarily unavailable/)).toBeTruthy();
  });

  it("shows a question that hasn't been evaluated yet", () => {
    const run = runDetail({ status: "running", questions_done: 1 });
    run.questions[1].results = [];
    render(<QuestionResults run={run} />);
    expect(within(screen.getByTestId("question-u01")).getByText("Not evaluated yet")).toBeTruthy();
  });

  it("shows the model text an answer without citations had", () => {
    const run = runDetail();
    run.questions[1].results[1] = {
      ...run.questions[1].results[1],
      outcome: "abstained",
      abstain_reason: "no_valid_citation",
      answer: "I couldn't find enough information in this knowledge base to answer that.",
      error_stage: null,
      error_category: null,
      error_message: null,
      abstention_correct: true,
      details: { model_text: "France won the 1998 World Cup." },
    };
    render(<QuestionResults run={run} />);
    expect(screen.getByText(/Withheld model text \(never shown in chat\)/)).toBeTruthy();
    expect(screen.getByText("France won the 1998 World Cup.")).toBeTruthy();
  });
});

describe("RunOverview", () => {
  it("shows progress while running", () => {
    render(<RunOverview run={runDetail({ status: "running", questions_done: 1, finished_at: null })} />);
    const progress = screen.getByRole("progressbar", { name: "Questions evaluated" });
    expect(progress.getAttribute("aria-valuenow")).toBe("1");
    expect(screen.getByText(/Evaluating 1 of 2 questions/)).toBeTruthy();
  });

  it("explains a stopped run and keeps its partial results", () => {
    render(
      <RunOverview
        run={runDetail({ status: "failed", questions_done: 1, error_message: "Daily quota used up." })}
      />,
    );
    expect(screen.getByRole("alert").textContent).toContain("stopped after 1 of 2 questions");
    expect(screen.getByText("Daily quota used up.")).toBeTruthy();
    expect(screen.getByText("Stopped")).toBeTruthy();
  });
});

describe("retry reservations", () => {
  it("shows additional retry reservations only when there were retries", () => {
    const run = runDetail();
    render(<RunOverview run={run} />);
    expect(screen.queryByText("Additional retry reservations")).toBeNull();
    cleanup();
    run.summary.retry_reservations = {
      count: 1,
      query_embeddings: 1,
      generations: 2,
      held_from_original: { query_embeddings: 0, generation_requests: 0 },
    };
    render(<RunOverview run={run} />);
    expect(screen.getByText("Additional retry reservations")).toBeTruthy();
    expect(screen.getByText(/1 retried question\(s\): 1 query embeddings, 2 generations/)).toBeTruthy();
  });
});

describe("DatasetPanel and StartRun", () => {
  it("explains missing demo documents and blocks the run", () => {
    const missing = overview({
      ready: false,
      sources: [
        { key: "product-guide.md", status: "missing", chunks: 0 },
        { key: "returns-policy.md", status: "content_differs", chunks: 2 },
      ],
    });
    render(withQueryClient(<><DatasetPanel overview={missing} /><StartRun overview={missing} /></>));
    expect(screen.getByText("Not uploaded")).toBeTruthy();
    expect(screen.getByText("Content differs")).toBeTruthy();
    expect(screen.getByTestId("corpus-help").textContent).toContain("sample_data/demo_store");
    expect((screen.getByRole("button", { name: /Start evaluation/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the Gemini requests a run will reserve for each choice", () => {
    render(withQueryClient(<StartRun overview={overview()} />));
    expect(screen.getByTestId("estimate").textContent).toContain("26 query embeddings and 26 answer");
    fireEvent.click(screen.getByLabelText(/Retrieval, vector and hybrid answers/));
    expect(screen.getByTestId("estimate").textContent).toContain("26 query embeddings and 52 answer");
    fireEvent.click(screen.getByLabelText(/Retrieval only/));
    expect(screen.getByTestId("estimate").textContent).toContain("and 0 answer");
  });

  it("starts a run with the chosen answer modes and opens it", async () => {
    const fetch = vi.fn(async () =>
      new Response(JSON.stringify(runDetail({ id: "new-run", status: "queued" })), { status: 202 }),
    );
    vi.stubGlobal("fetch", fetch);
    render(withQueryClient(<StartRun overview={overview()} />));
    fireEvent.click(screen.getByLabelText(/Retrieval, vector and hybrid answers/));
    fireEvent.click(screen.getByRole("button", { name: /Start evaluation/ }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/evaluations/new-run"));
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://api.test/api/v1/evaluations/run");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ answer_modes: ["vector", "hybrid"] });
    expect(init.credentials).toBe("include");
  });

  it("shows the API's reason when a run can't start", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              error: { code: "evaluation_running", message: "An evaluation is already running here." },
            }),
            { status: 409 },
          ),
      ),
    );
    render(withQueryClient(<StartRun overview={overview()} />));
    fireEvent.click(screen.getByRole("button", { name: /Start evaluation/ }));
    expect(await screen.findByText("An evaluation is already running here.")).toBeTruthy();
    expect(screen.getByText("The evaluation didn't start")).toBeTruthy();
    expect(push).not.toHaveBeenCalled();
  });

  it("points to the active run instead of starting another", () => {
    render(withQueryClient(<StartRun overview={overview()} activeRunId="run-9" />));
    expect(screen.getByRole("link", { name: "Follow its progress" }).getAttribute("href")).toBe(
      "/evaluations/run-9",
    );
    expect((screen.getByRole("button", { name: /Start evaluation/ }) as HTMLButtonElement).disabled).toBe(true);
  });
});
