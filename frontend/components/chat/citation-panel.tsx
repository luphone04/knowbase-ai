"use client";

import { ExternalLink } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { excerptParagraphs, pageLabel } from "@/lib/citations";
import type { Citation } from "@/lib/types";

/** The excerpt a citation points to, as saved with the answer. */
export function CitationPanel({
  citation,
  onClose,
}: {
  citation: Citation | null;
  onClose: () => void;
}) {
  const page = citation ? pageLabel(citation) : null;
  return (
    <Sheet open={citation !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full gap-0 sm:max-w-lg">
        {citation ? (
          <>
            <SheetHeader className="border-b">
              <p className="text-xs font-medium text-blue-700">Source {citation.id}</p>
              <SheetTitle className="pr-6">{citation.document_title}</SheetTitle>
              <SheetDescription>
                {[citation.filename ?? citation.source_name, page].filter(Boolean).join(" · ")}
              </SheetDescription>
            </SheetHeader>
            <div className="flex-1 overflow-y-auto p-4">
              {citation.heading ? (
                <p className="mb-2 text-sm font-semibold">{citation.heading}</p>
              ) : null}
              <blockquote className="grid gap-2 border-l-2 border-blue-200 pl-3 text-sm leading-relaxed">
                {excerptParagraphs(citation.excerpt).map((paragraph, i) => (
                  <p key={i}>{paragraph}</p>
                ))}
              </blockquote>
              <p className="mt-4 text-xs text-muted-foreground">
                The passage retrieved and given to the model with this question, saved with the
                answer.
              </p>
            </div>
            <div className="border-t p-4">
              <Button variant="outline" size="sm" asChild>
                <Link href={`/sources/${citation.source_id}`}>
                  <ExternalLink />
                  Open document
                </Link>
              </Button>
            </div>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
