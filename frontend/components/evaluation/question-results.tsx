import { StatusBadge } from "@/components/status-badge";
import { Score } from "@/components/evaluation/score";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  abstainReasonLabel,
  categoryLabel,
  decisionLabel,
  formatMs,
  outcome,
  variantDetails,
} from "@/lib/evaluation";
import type { EvaluationQuestion, EvaluationResult, EvaluationRunDetail } from "@/lib/types";

/** Every question: what was expected, what each configuration retrieved, and what was answered. */
export function QuestionResults({ run }: { run: EvaluationRunDetail }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>Per-question results</h2>
        </CardTitle>
        <CardDescription>
          Open a question to see its expected documents and reference facts, the documents each
          configuration retrieved (ranked), and the answer, citations, and decision.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-2">
        {run.questions.map(({ question, results }) => (
          <QuestionItem key={question.id} run={run} question={question} results={results} />
        ))}
      </CardContent>
    </Card>
  );
}

function QuestionItem({
  run,
  question,
  results,
}: {
  run: EvaluationRunDetail;
  question: EvaluationQuestion;
  results: EvaluationResult[];
}) {
  const vector = results.find((r) => r.variant === "vector");
  const hybrid = results.find((r) => r.variant === "hybrid");
  const answered = results.filter((r) => r.answer_evaluated);
  const failed = results.some((r) => r.error_stage);
  return (
    <details className="group rounded-lg border" data-testid={`question-${question.key}`}>
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
        <span className="w-8 shrink-0 font-mono text-xs text-muted-foreground">{question.key}</span>
        <span className="min-w-0 flex-1 font-medium">{question.question}</span>
        <span className="flex flex-wrap items-center gap-1.5 text-xs">
          <Badge variant="outline">{question.answerable ? categoryLabel(question.category) : "Unanswerable"}</Badge>
          {results.length === 0 ? (
            <span className="text-muted-foreground">Not evaluated yet</span>
          ) : question.answerable ? (
            <span className="tabular-nums text-muted-foreground">
              RR vector <Score value={vector?.reciprocal_rank} /> · hybrid{" "}
              <Score value={hybrid?.reciprocal_rank} />
            </span>
          ) : null}
          {answered.map((r) => (
            <StatusBadge key={r.variant} {...outcome(r)} />
          ))}
          {failed ? <StatusBadge label="Error" tone="danger" /> : null}
        </span>
      </summary>
      <div className="grid gap-4 border-t px-3 py-3 text-sm">
        <Expected question={question} />
        {results.length === 0 ? (
          <p className="text-muted-foreground">This question hasn&apos;t been evaluated yet.</p>
        ) : (
          <>
            <section className="grid gap-2">
              <h3 className="font-semibold">Retrieved documents</h3>
              <p className="text-xs text-muted-foreground">
                Search query: <q>{results[0].retrieval_query}</q>
                {results[0].embedding_latency_ms != null
                  ? ` · query embedding ${formatMs(results[0].embedding_latency_ms)}`
                  : null}
              </p>
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                {results.map((result) => (
                  <Retrieved key={result.variant} label={variantDetails(run, result.variant).label} result={result} />
                ))}
              </div>
            </section>
            {answered.map((result) => (
              <AnswerResult key={result.variant} result={result} answerable={question.answerable} />
            ))}
          </>
        )}
      </div>
    </details>
  );
}

function Expected({ question }: { question: EvaluationQuestion }) {
  return (
    <section className="grid gap-1.5">
      {question.previous_question ? (
        <p>
          <span className="text-muted-foreground">Previous question: </span>
          {question.previous_question}
        </p>
      ) : null}
      {question.answerable ? (
        <>
          <p>
            <span className="text-muted-foreground">Expected documents: </span>
            {question.expected_source_keys.join(", ")}
          </p>
          <p>
            <span className="text-muted-foreground">Reference answer: </span>
            {question.expected_answer}
          </p>
          <ul className="grid gap-1 text-xs text-muted-foreground">
            {question.evidence.map((e, i) => (
              <li key={i}>
                {e.source_key}: <q>{e.quote}</q>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p>
          <span className="text-muted-foreground">Unanswerable: </span>
          the corpus contains none of: {question.absent_terms.join(", ")}.
        </p>
      )}
      {question.notes ? <p className="text-xs text-muted-foreground">{question.notes}</p> : null}
    </section>
  );
}

function Retrieved({ label, result }: { label: string; result: EvaluationResult }) {
  return (
    <div className="grid content-start gap-1.5 rounded-md border p-2" data-testid={`retrieved-${result.variant}`}>
      <p className="text-xs font-medium">{label}</p>
      {result.error_stage === "embedding" ? (
        <p className="text-xs text-red-700">Not retrieved: {result.error_message}</p>
      ) : (
        <>
          <ol className="grid gap-0.5 text-xs">
            {result.retrieved_sources.map((source) => (
              <li key={`${source.rank}-${source.name}`} className="flex gap-1.5">
                <span className="w-4 shrink-0 tabular-nums text-muted-foreground">{source.rank}.</span>
                <span className={source.relevant ? "font-medium text-emerald-700" : undefined}>
                  {source.name}
                  {source.relevant ? " (expected)" : null}
                  {source.source_key === null ? " (not in the dataset)" : null}
                </span>
                <span className="text-muted-foreground">chunks {source.chunk_ranks.join(", ")}</span>
              </li>
            ))}
          </ol>
          <p className="text-xs text-muted-foreground">
            {result.hit_at_5 === null ? (
              "No relevant document (unanswerable)"
            ) : (
              <>
                Hit {result.hit_at_5 ? "yes" : "no"} · recall <Score value={result.recall_at_5} /> · RR{" "}
                <Score value={result.reciprocal_rank} />
              </>
            )}
            {result.lexical_candidates !== null ? ` · keyword matches ${result.lexical_candidates}` : null}
            {result.top_vector_distance !== null
              ? ` · nearest distance ${result.top_vector_distance.toFixed(3)}`
              : null}
            {` · ${formatMs(result.retrieval_latency_ms)}`}
          </p>
        </>
      )}
    </div>
  );
}

function AnswerResult({ result, answerable }: { result: EvaluationResult; answerable: boolean }) {
  const decision = decisionLabel(result.abstention_correct);
  const mode = result.variant === "vector" ? "Vector only" : "Hybrid";
  return (
    <section className="grid gap-1.5 rounded-md border p-2" data-testid={`answer-${result.variant}`}>
      <h3 className="flex flex-wrap items-center gap-2 font-semibold">
        Answer ({mode})
        <StatusBadge {...outcome(result)} />
        {decision ? <StatusBadge {...decision} /> : null}
      </h3>
      {result.outcome === "provider_error" ? (
        <>
          <p className="text-red-700">
            {result.error_category === "skipped" ? "" : `${result.error_category}: `}
            {result.error_message}
          </p>
          {result.error_category === "skipped" ? null : (
            <p className="text-xs text-muted-foreground">
              Failed after{" "}
              {typeof result.details.failed_attempt_ms === "number"
                ? formatMs(result.details.failed_attempt_ms)
                : "an unmeasured time (not recorded for this run)"}
            </p>
          )}
        </>
      ) : (
        <>
          <p className="whitespace-pre-wrap">{result.answer}</p>
          {result.abstain_reason ? (
            <p className="text-xs text-muted-foreground">
              Abstained: {abstainReasonLabel(result.abstain_reason)}.
            </p>
          ) : null}
          {result.details.model_text && result.abstain_reason === "no_valid_citation" ? (
            <p className="text-xs text-muted-foreground">
              Withheld model text (never shown in chat): <q>{result.details.model_text}</q>
            </p>
          ) : null}
          {result.citations.length ? (
            <p className="text-xs">
              <span className="text-muted-foreground">Citations: </span>
              {result.citations
                .map((c) => `${c.id} ${c.filename ?? c.source_name}${c.source_key ? "" : " (not in the dataset)"}`)
                .join(" · ")}
            </p>
          ) : null}
          {answerable && result.outcome === "answered" ? (
            <p className="text-xs text-muted-foreground">
              Citation precision <Score value={result.citation_precision} /> · recall{" "}
              <Score value={result.citation_recall} />
            </p>
          ) : null}
          <p className="text-xs text-muted-foreground">
            End to end {formatMs(result.answer_latency_ms)}
            {typeof result.token_usage.generation_latency_ms === "number"
              ? ` · generation ${formatMs(result.token_usage.generation_latency_ms)}`
              : null}
          </p>
        </>
      )}
    </section>
  );
}
