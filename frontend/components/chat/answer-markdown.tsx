"use client";

import Markdown from "react-markdown";

import { citationIdFromHref, isSafeExternalUrl, linkCitations } from "@/lib/citations";
import type { Citation } from "@/lib/types";

/**
 * A model answer as Markdown. react-markdown never renders raw HTML; images are
 * dropped (they would load remote URLs); [Sn] markers become citation buttons.
 */
export function AnswerMarkdown({
  content,
  citations,
  onCite,
}: {
  content: string;
  citations: Citation[];
  onCite: (citation: Citation) => void;
}) {
  const byId = new Map(citations.map((c) => [c.id, c]));
  return (
    <div className="text-sm leading-relaxed [&_li]:my-1 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-2 [&_p:first-child]:mt-0 [&_p:last-child]:mb-0 [&_strong]:font-semibold [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_code]:rounded [&_code]:bg-muted [&_code]:px-1">
      <Markdown
        disallowedElements={["img"]}
        unwrapDisallowed
        components={{
          a: ({ href, children }) => {
            const citation = byId.get(citationIdFromHref(href) ?? "");
            if (citation) return <CitationChip citation={citation} onSelect={onCite} />;
            if (isSafeExternalUrl(href)) {
              return (
                <a
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer nofollow"
                  className="underline underline-offset-4"
                >
                  {children}
                </a>
              );
            }
            return <>{children}</>;
          },
        }}
      >
        {linkCitations(content, citations)}
      </Markdown>
    </div>
  );
}

export function CitationChip({
  citation,
  onSelect,
}: {
  citation: Citation;
  onSelect: (citation: Citation) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(citation)}
      aria-label={`Source ${citation.id}: ${citation.source_name}`}
      className="mx-0.5 inline-flex h-5 items-center rounded-md border border-blue-200 bg-blue-50 px-1.5 align-baseline text-xs font-medium text-blue-700 hover:bg-blue-100 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
    >
      {citation.id}
    </button>
  );
}
