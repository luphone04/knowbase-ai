"use client";

import { SearchX } from "lucide-react";

import { AnswerMarkdown, CitationChip } from "@/components/chat/answer-markdown";
import { pageLabel } from "@/lib/citations";
import type { AbstainReason, Citation, Message } from "@/lib/types";

const ABSTAIN_REASONS: Record<AbstainReason, string> = {
  no_evidence: "Nothing in your indexed documents matched this question.",
  model_insufficient: "The passages that were found don't answer this question.",
  no_valid_citation:
    "A draft answer couldn't be tied to any source passage, so it isn't shown.",
};

export function UserMessage({ content }: { content: string }) {
  return (
    <div className="flex justify-end">
      <p className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary px-3.5 py-2 text-sm whitespace-pre-wrap text-primary-foreground">
        {content}
      </p>
    </div>
  );
}

export function AssistantMessage({
  message,
  onCite,
}: {
  message: Message;
  onCite: (citation: Citation) => void;
}) {
  if (message.outcome === "abstained") {
    return (
      <div
        data-testid="assistant-message"
        data-outcome="abstained"
        className="max-w-[85%] rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-3"
      >
        <p className="flex items-center gap-2 text-sm font-semibold text-amber-900">
          <SearchX className="size-4" aria-hidden />
          Not found in your documents
        </p>
        <p className="mt-1 text-sm text-amber-900">{message.content}</p>
        {message.abstain_reason ? (
          <p className="mt-1 text-xs text-amber-800">{ABSTAIN_REASONS[message.abstain_reason]}</p>
        ) : null}
        <RetrievalNote message={message} />
      </div>
    );
  }

  return (
    <div
      data-testid="assistant-message"
      data-outcome="answered"
      className="max-w-[85%] rounded-lg border bg-card px-3.5 py-3"
    >
      <AnswerMarkdown content={message.content} citations={message.citations} onCite={onCite} />
      {message.citations.length > 0 ? (
        <div className="mt-3 border-t pt-2">
          <p className="text-xs font-medium text-muted-foreground">Sources</p>
          <ul className="mt-1 grid gap-1">
            {message.citations.map((citation) => (
              <li key={citation.id} className="flex min-w-0 items-center gap-1.5 text-xs">
                <CitationChip citation={citation} onSelect={onCite} />
                <span className="truncate" title={citation.source_name}>
                  {citation.filename ?? citation.source_name}
                </span>
                {pageLabel(citation) ? (
                  <span className="shrink-0 text-muted-foreground">· {pageLabel(citation)}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <RetrievalNote message={message} />
    </div>
  );
}

/** How the answer was found, from the metadata the API saved with it. */
function RetrievalNote({ message }: { message: Message }) {
  const meta = message.metadata;
  const parts: string[] = [];
  if (meta.retrieval_mode === "hybrid") parts.push("Hybrid search");
  else if (meta.retrieval_mode === "vector") parts.push("Vector search");
  if (typeof meta.excerpts === "number") {
    parts.push(`${meta.excerpts} ${meta.excerpts === 1 ? "excerpt" : "excerpts"}`);
  }
  if (message.latency_ms !== null) parts.push(`${(message.latency_ms / 1000).toFixed(1)} s`);
  if (meta.contextualized === true) parts.push("follow-up: searched with your previous question");
  if (parts.length === 0) return null;
  return <p className="mt-2 text-xs text-muted-foreground">{parts.join(" · ")}</p>;
}
