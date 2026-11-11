"use client";

import { ChevronRight } from "lucide-react";
import { useId, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * A card whose body can be collapsed (WAI-ARIA disclosure pattern).
 *
 * The header is a heading wrapping one native button, so Enter and Space toggle it and
 * `aria-expanded` / `aria-controls` describe it. The button is named by the title alone; the
 * badges and the summary (shown open or closed) are its description. A collapsed body stays
 * mounted but `hidden`, so anything opened inside it is still open when the card reopens.
 */
export function CollapsibleCard({
  title,
  badges,
  summary,
  description,
  defaultOpen = true,
  contentClassName,
  children,
  ...props
}: {
  title: React.ReactNode;
  /** Status badges next to the title, visible open or closed. */
  badges?: React.ReactNode;
  /** A one-line summary of the key results, visible open or closed. */
  summary?: React.ReactNode;
  /** Explanatory text, shown only when open. */
  description?: React.ReactNode;
  defaultOpen?: boolean;
  contentClassName?: string;
  children: React.ReactNode;
} & Omit<React.ComponentProps<"div">, "title" | "children">) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  const titleId = `${id}-title`;
  const badgesId = `${id}-badges`;
  const summaryId = `${id}-summary`;
  const contentId = `${id}-content`;
  const describedBy = [badges ? badgesId : null, summary ? summaryId : null].filter(Boolean).join(" ");

  return (
    <Card data-state={open ? "open" : "closed"} {...props}>
      <CardHeader>
        <h2 className="font-heading text-base leading-snug font-medium">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={contentId}
            aria-labelledby={titleId}
            aria-describedby={describedBy || undefined}
            onClick={() => setOpen((value) => !value)}
            className="-m-1 flex w-[calc(100%+0.5rem)] cursor-pointer items-start gap-2 rounded-md p-1 text-left outline-none hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            <ChevronRight
              aria-hidden
              className={cn(
                "mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none",
                open && "rotate-90",
              )}
            />
            <span className="flex min-w-0 flex-1 flex-col gap-1">
              <span className="flex flex-wrap items-center gap-2">
                <span id={titleId}>{title}</span>
                {badges ? (
                  <span id={badgesId} className="flex flex-wrap items-center gap-1.5">
                    {badges}
                  </span>
                ) : null}
              </span>
              {summary ? (
                <span id={summaryId} className="text-sm font-normal text-muted-foreground">
                  {summary}
                </span>
              ) : null}
            </span>
          </button>
        </h2>
      </CardHeader>
      <div
        id={contentId}
        role="region"
        aria-labelledby={titleId}
        hidden={!open}
        className="grid gap-(--card-spacing)"
      >
        {description ? <CardDescription className="px-(--card-spacing)">{description}</CardDescription> : null}
        <CardContent className={contentClassName}>{children}</CardContent>
      </div>
    </Card>
  );
}
