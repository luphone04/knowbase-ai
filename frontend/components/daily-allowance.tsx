"use client";

import { InfoTip } from "@/components/info-tip";
import { allowanceHelp, allowanceText, exhaustedText } from "@/lib/limits";
import { useDailyLimits } from "@/lib/queries";
import { cn } from "@/lib/utils";

/**
 * A quiet "24 of 30 questions left today", with an ⓘ that explains the limit. Once nothing
 * is left it says when more become available. The numbers are the API's (GET /limits).
 */
export function DailyAllowance({ className }: { className?: string }) {
  const limits = useDailyLimits();
  const base = cn("flex min-h-6 items-center gap-0.5 text-xs text-muted-foreground", className);

  if (limits.isPending) {
    return (
      <p className={base} data-testid="allowance-chat" aria-busy="true">
        <span className="sr-only">Loading today&apos;s allowance…</span>
      </p>
    );
  }
  const allowance = limits.data?.chat;
  if (limits.isError || typeof allowance?.remaining !== "number") {
    return (
      <p className={base} data-testid="allowance-chat">
        Today&apos;s allowance is unavailable right now.
      </p>
    );
  }
  const help = <InfoTip label="the daily question limit">{allowanceHelp(allowance)}</InfoTip>;
  if (allowance.remaining === 0) {
    return (
      <p
        role="status"
        className={cn(base, "text-amber-800 dark:text-amber-300")}
        data-testid="allowance-chat"
      >
        <span>{exhaustedText(allowance, limits.data.resets_at)}</span>
        {help}
      </p>
    );
  }
  return (
    <p className={base} data-testid="allowance-chat">
      <span className="tabular-nums">{allowanceText(allowance)}</span>
      {help}
    </p>
  );
}
