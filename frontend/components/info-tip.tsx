"use client";

import { Info } from "lucide-react";
import { Tooltip } from "radix-ui";
import { useId, useRef, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * A small ⓘ button that explains a term in a tooltip.
 *
 * Opens on mouse hover and on keyboard focus (Radix Tooltip), and toggles on tap or click so it
 * works on touch screens, where hover doesn't exist. Escape, blur, or a tap elsewhere closes it.
 * The tooltip is portalled to the page body and kept inside the viewport, so a scrolling
 * table or the screen edge never clips it. Screen readers get the label as the button's name
 * and the explanation as its description, always (not only while the tooltip is shown).
 */
export function InfoTip({
  label,
  children,
  delayMs = 150,
  className,
}: {
  /** What is explained, e.g. "MRR": the button is named "About MRR". */
  label: string;
  /** The explanation (plain text). */
  children: string;
  delayMs?: number;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const wasOpen = useRef(false);
  const descriptionId = useId();

  return (
    <Tooltip.Provider delayDuration={delayMs}>
      <Tooltip.Root open={open} onOpenChange={setOpen}>
        <Tooltip.Trigger
          asChild
          // On the Trigger (not the button) so Radix runs these first and honours
          // preventDefault. Radix closes an open tooltip on pointer down; remember whether it
          // was open so the click that follows toggles it (tap to open, tap again to close).
          onPointerDown={() => {
            wasOpen.current = open;
          }}
          onClick={(event) => {
            event.preventDefault(); // keep Radix from closing it right after the toggle
            const opened = event.detail === 0 ? open : wasOpen.current; // detail 0: keyboard
            setOpen(!opened);
          }}
        >
          <button
            type="button"
            aria-label={`About ${label}`}
            // Replaces Radix's open-only description, so the explanation is always announced.
            aria-describedby={descriptionId}
            className={cn(
              "-my-1 inline-flex size-6 shrink-0 cursor-help items-center justify-center rounded-full align-middle text-muted-foreground outline-none hover:text-foreground focus-visible:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 data-[state=delayed-open]:text-foreground data-[state=instant-open]:text-foreground",
              className,
            )}
          >
            <Info aria-hidden className="size-3.5" />
          </button>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content
            side="top"
            sideOffset={6}
            collisionPadding={8}
            className="z-50 max-w-[min(18rem,calc(100vw-1rem))] rounded-md border bg-popover px-3 py-2 text-left text-xs leading-snug font-normal whitespace-normal text-popover-foreground shadow-md animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 motion-reduce:animate-none"
          >
            {children}
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
      {/* Hidden, but still read as the description: keeps it out of a table header's name. */}
      <span id={descriptionId} hidden>
        {children}
      </span>
    </Tooltip.Provider>
  );
}
