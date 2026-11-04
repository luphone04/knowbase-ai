// Citation markers in answers. The backend keeps only ids it assigned and writes
// each group as "[S1]" or "[S1, S2]" (backend/app/chat/citations.py).

import type { Citation } from "./types";

const GROUP = /\[(S\d+(?:\s*,\s*S\d+)*)\]/g;
export const CITATION_HREF_PREFIX = "#cite-";

/**
 * Turns each known marker into a Markdown link ("[S1](#cite-S1)") that the answer
 * renderer shows as a citation button. Ids with no citation stay plain text.
 */
export function linkCitations(content: string, citations: Pick<Citation, "id">[]): string {
  const known = new Set(citations.map((c) => c.id));
  return content.replace(GROUP, (group: string, body: string) => {
    const ids = body.split(/\s*,\s*/);
    if (!ids.some((id) => known.has(id))) return group;
    return ids
      .map((id) => (known.has(id) ? `[${id}](${CITATION_HREF_PREFIX}${id})` : id))
      .join(" ");
  });
}

/** The citation id behind a link made by linkCitations, or null for any other link. */
export function citationIdFromHref(href: string | undefined): string | null {
  if (!href?.startsWith(CITATION_HREF_PREFIX)) return null;
  const id = href.slice(CITATION_HREF_PREFIX.length);
  return /^S\d+$/.test(id) ? id : null;
}

/** Only absolute http(s) links are rendered as links. */
export function isSafeExternalUrl(href: string | undefined): boolean {
  return !!href && /^https?:\/\/[^\s]+$/i.test(href);
}

/**
 * The excerpt's paragraphs for display. Source files are often hard-wrapped, so single
 * line breaks are joined; blank lines still separate paragraphs. The text is unchanged.
 */
export function excerptParagraphs(excerpt: string): string[] {
  return excerpt
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.replace(/\s*\n\s*/g, " ").trim())
    .filter(Boolean);
}

/** "Page 3", "Pages 3–4", or null when the source has no pages (TXT, MD, DOCX). */
export function pageLabel(citation: Pick<Citation, "page_start" | "page_end">): string | null {
  const { page_start: start, page_end: end } = citation;
  if (start === null) return null;
  if (end === null || end === start) return `Page ${start}`;
  return `Pages ${start}–${end}`;
}
