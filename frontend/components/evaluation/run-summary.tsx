import { Latency, Score } from "@/components/evaluation/score";
import { CollapsibleCard } from "@/components/collapsible-card";
import { InfoTip } from "@/components/info-tip";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { abstainReasonLabel, formatScore, variantDetails, variantKeys } from "@/lib/evaluation";
import type { AnswerMode, AnswerSummary, EvaluationRun } from "@/lib/types";

const MODE_LABEL: Record<AnswerMode, string> = { vector: "Vector only", hybrid: "Hybrid" };

/** What each retrieval column means, in plain language (shown in the column header's ⓘ). */
export const RETRIEVAL_METRIC_HELP = {
  "HitRate@5":
    "How often at least one correct document appears in the top 5 results. Higher is better; 1.00 means 100% of questions.",
  "Recall@5":
    "How many of the expected documents were found in the top 5 results. Higher is better; 1.00 means all expected documents were retrieved.",
  MRR: "Mean Reciprocal Rank. Measures how highly the first correct document is ranked. Rank #1 = 1.00, rank #2 = 0.50. Higher is better.",
  "No keyword match":
    "Number of questions where keyword search found zero matches. The vector search may still find relevant documents. Lower generally means broader keyword coverage, not necessarily better accuracy.",
} as const;

function MetricHead({ name }: { name: keyof typeof RETRIEVAL_METRIC_HELP }) {
  return (
    <TableHead className="text-right">
      <span className="inline-flex items-center gap-0.5">
        {name}
        <InfoTip label={name}>{RETRIEVAL_METRIC_HELP[name]}</InfoTip>
      </span>
    </TableHead>
  );
}

/** HitRate@5, Recall@5, and MRR per retrieval configuration, from the run's stored results. */
export function RetrievalComparison({ run, defaultOpen = true }: { run: EvaluationRun; defaultOpen?: boolean }) {
  const retrieval = run.summary.retrieval ?? {};
  const keys = variantKeys(run);
  const chance = retrieval.vector?.chance_hit_rate_at_5 ?? retrieval.hybrid?.chance_hit_rate_at_5;
  const retrievalErrors = retrieval.vector?.errors ?? 0;
  const mrr = keys.map((key) => `${variantDetails(run, key).label} ${formatScore(retrieval[key]?.mrr) ?? "—"}`);
  return (
    <CollapsibleCard
      title="Retrieval comparison"
      defaultOpen={defaultOpen}
      data-testid="section-retrieval"
      summary={`MRR: ${mrr.join(" · ")}`}
      description={
        <>
          Source level: the top 5 chunks are collapsed to their documents, best rank first.
          Averaged over answerable questions. Unanswerable questions have no relevant document
          and are not in these averages.
        </>
      }
      contentClassName="grid gap-3"
    >
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Configuration</TableHead>
              <MetricHead name="HitRate@5" />
              <MetricHead name="Recall@5" />
              <MetricHead name="MRR" />
              <TableHead className="text-right">Questions</TableHead>
              <MetricHead name="No keyword match" />
              <TableHead>Search latency</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {keys.map((key) => {
              const details = variantDetails(run, key);
              const row = retrieval[key];
              const noMatch = row?.no_lexical_candidates;
              return (
                <TableRow key={key} data-testid={`retrieval-${key}`}>
                  <TableCell className="min-w-56">
                    <span className="flex flex-wrap items-center gap-1.5">
                      <span className="font-medium">{details.label}</span>
                      {details.experimental ? (
                        <Badge variant="outline">Experiment</Badge>
                      ) : (
                        <Badge variant="secondary">Production mode</Badge>
                      )}
                    </span>
                  </TableCell>
                  <TableCell className="text-right">
                    <Score value={row?.hit_rate_at_5} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Score value={row?.recall_at_5} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Score value={row?.mrr} />
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {row ? row.questions : "—"}
                    {row && row.errors > 0 ? (
                      <span className="block text-xs text-red-700">{row.errors} failed</span>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {noMatch?.answerable == null ? (
                      "—"
                    ) : (
                      <>
                        {noMatch.answerable} answerable
                        <br />
                        {noMatch.unanswerable ?? 0} unanswerable
                      </>
                    )}
                  </TableCell>
                  <TableCell className="text-xs">
                    <Latency stats={row?.latency_ms} />
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
      <ul className="grid gap-1 text-xs text-muted-foreground">
        <li>
          A random ranking of this corpus would reach HitRate@5 ≈{" "}
          <span className="font-medium text-foreground">{formatScore(chance) ?? "—"}</span>: with
          so few chunks, the top 5 holds a large share of the corpus, so HitRate@5 and Recall@5
          are close to saturated, and MRR separates the configurations better.
        </li>
        <li>
          “No keyword match”: questions for which the keyword list found no chunk at all, so
          hybrid ranking came from vectors alone. Experiments are measured here only; production
          keeps keyword AND (ADR 0009).
        </li>
        <li>
          Search latency is the database search alone, after the shared query embedding
          (successful embeddings: <Latency stats={run.summary.embedding_latency_ms ?? undefined} />
          {retrievalErrors > 0 ? (
            <>
              ; failed embeddings, until the error:{" "}
              <Latency stats={run.summary.failed_embedding_latency_ms ?? undefined} />
            </>
          ) : null}
          ).
        </li>
      </ul>
    </CollapsibleCard>
  );
}

/** Abstention accuracy, citations, and latency per answer mode the run evaluated. */
export function AnswerEvaluation({ run, defaultOpen = true }: { run: EvaluationRun; defaultOpen?: boolean }) {
  const answers = run.summary.answers ?? {};
  const modes: AnswerMode[] = ["vector", "hybrid"];
  const measured = modes.flatMap((mode) => {
    const summary = answers[mode];
    if (!summary) return [];
    const errors = summary.provider_errors ? ` · ${summary.provider_errors} provider error(s)` : "";
    return [
      `${MODE_LABEL[mode]}: abstention accuracy ${formatScore(summary.abstention_accuracy) ?? "—"} · citation precision ${formatScore(summary.citation_precision) ?? "—"}${errors}`,
    ];
  });
  return (
    <CollapsibleCard
      title="Answer evaluation"
      defaultOpen={defaultOpen}
      data-testid="section-answers"
      summary={measured.length ? measured.join("; ") : "Not measured: this run evaluated retrieval only."}
      description={
        <>
          The real chat pipeline (prompt, Gemini, citation validation, abstention), without saving
          a conversation. Citation scores are source level, over answered answerable questions: a
          relevant citation doesn&apos;t prove that every sentence is supported. A provider error is
          never counted as an abstention.
        </>
      }
      contentClassName="grid gap-4 lg:grid-cols-2"
    >
      {modes.map((mode) => {
        const summary = answers[mode];
        return summary ? (
          <AnswerModeSummary key={mode} mode={mode} summary={summary} />
        ) : (
          <section key={mode} className="rounded-lg border p-3" data-testid={`answers-${mode}`}>
            <h3 className="text-sm font-semibold">{MODE_LABEL[mode]}</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              Not measured: this run didn&apos;t answer with {MODE_LABEL[mode].toLowerCase()}{" "}
              retrieval.
            </p>
          </section>
        );
      })}
    </CollapsibleCard>
  );
}

function AnswerModeSummary({ mode, summary }: { mode: AnswerMode; summary: AnswerSummary }) {
  const { answerable, unanswerable } = summary.confusion;
  const reasons = Object.entries(summary.abstain_reasons);
  return (
    <section className="grid gap-3 rounded-lg border p-3" data-testid={`answers-${mode}`}>
      <h3 className="text-sm font-semibold">{MODE_LABEL[mode]}</h3>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
        <Metric label="Abstention accuracy" detail={`over ${summary.decided} decided`}>
          <Score value={summary.abstention_accuracy} />
        </Metric>
        <Metric label="Citation precision" detail={`over ${summary.citation_questions} answered`}>
          <Score value={summary.citation_precision} />
        </Metric>
        <Metric label="Citation recall" detail={`over ${summary.citation_questions} answered`}>
          <Score value={summary.citation_recall} />
        </Metric>
        <Metric label="Provider errors">
          <span className={summary.provider_errors ? "font-medium text-red-700" : "font-medium"}>
            {summary.provider_errors}
          </span>
        </Metric>
        <Metric label="Answer latency (successful answers)" wide>
          <Latency stats={summary.latency_ms} />
        </Metric>
        {summary.provider_errors > 0 ? (
          <Metric label="Failed attempts, until the error" wide>
            <Latency stats={summary.failed_attempt_latency_ms ?? undefined} />
          </Metric>
        ) : null}
      </dl>
      <table className="w-full text-sm" aria-label={`${MODE_LABEL[mode]} decisions`}>
        <thead>
          <tr className="text-left text-xs text-muted-foreground">
            <th className="font-medium">Expected</th>
            <th className="text-right font-medium">Answered</th>
            <th className="text-right font-medium">Abstained</th>
            <th className="text-right font-medium">Provider error</th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          <tr>
            <th className="text-left font-normal">Answerable</th>
            <td className="text-right font-medium text-emerald-700">{answerable.answered}</td>
            <td className="text-right">{answerable.abstained}</td>
            <td className="text-right">{answerable.provider_error}</td>
          </tr>
          <tr>
            <th className="text-left font-normal">Unanswerable</th>
            <td className="text-right">{unanswerable.answered}</td>
            <td className="text-right font-medium text-emerald-700">{unanswerable.abstained}</td>
            <td className="text-right">{unanswerable.provider_error}</td>
          </tr>
        </tbody>
      </table>
      {reasons.length ? (
        <p className="text-xs text-muted-foreground">
          Abstentions: {reasons.map(([reason, n]) => `${n} × ${abstainReasonLabel(reason)}`).join("; ")}
          .
        </p>
      ) : null}
    </section>
  );
}

function Metric({
  label,
  detail,
  wide,
  children,
}: {
  label: string;
  detail?: string;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={wide ? "col-span-2 sm:col-span-3" : undefined}>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd>
        {children}
        {detail ? <span className="ml-1 text-xs text-muted-foreground">({detail})</span> : null}
      </dd>
    </div>
  );
}
