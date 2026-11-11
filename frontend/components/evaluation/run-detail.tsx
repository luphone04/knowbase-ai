import { QuestionResults } from "@/components/evaluation/question-results";
import { RunOverview } from "@/components/evaluation/run-overview";
import { AnswerEvaluation, RetrievalComparison } from "@/components/evaluation/run-summary";
import { UsageCost } from "@/components/evaluation/usage-cost";
import type { EvaluationRunDetail } from "@/lib/types";

/**
 * An evaluation run's sections. Each one collapses; only the overview starts open, so a long
 * run reads as a list of summaries first. Any number of sections (and questions) can be open.
 */
export function RunDetail({ run }: { run: EvaluationRunDetail }) {
  return (
    <div className="grid gap-6 p-4 md:p-6">
      <RunOverview run={run} defaultOpen />
      {run.questions_done > 0 ? (
        <>
          <RetrievalComparison run={run} defaultOpen={false} />
          <AnswerEvaluation run={run} defaultOpen={false} />
          <UsageCost run={run} defaultOpen={false} />
        </>
      ) : null}
      <QuestionResults run={run} defaultOpen={false} />
    </div>
  );
}
