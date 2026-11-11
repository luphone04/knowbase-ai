// Chat usage and estimated cost, as shown under a reply and for a whole conversation.
// Same rules as the evaluation's usage: reported token counts only; unknown is "Not
// reported", never 0; an estimate missing some calls is a lower bound ("≥ $…").

import { formatTokens, formatUsd } from "./evaluation";
import type { ChatUsageGroup, ChatUsageIssue } from "./types";

export const NOT_REPORTED = "Not reported";
export const NOT_AVAILABLE = "Not available";

/** "12,345", or "Not reported". */
export function tokensText(value: number | null | undefined): string {
  return formatTokens(value) ?? NOT_REPORTED;
}

/** "$0.000468", "≥ $0.000468" when some calls have no known cost, or "Not available". */
export function costText(group: Pick<ChatUsageGroup, "cost_usd" | "cost_complete">): string {
  const usd = formatUsd(group.cost_usd);
  if (usd === null) return NOT_AVAILABLE;
  return group.cost_complete ? usd : `≥ ${usd}`;
}

/** A short, plain sentence for each reason an estimate is incomplete. */
export function chatUsageNote(issue: ChatUsageIssue): string {
  switch (issue.code) {
    case "usage_missing":
      return issue.kind === "embedding"
        ? "Gemini doesn't report tokens for search embeddings, so they aren't in the cost."
        : `Gemini reported no token counts for ${issue.requests} answer request(s) (for example, a failed one), so they aren't in the cost.`;
    case "unmeasured_attempts":
      return `${issue.attempts} retried ${issue.kind === "embedding" ? "embedding" : "answer"} attempt(s) aren't measured or included.`;
    case "cost_unavailable":
      return `${issue.requests} request(s) were saved before prices were recorded, so they have no cost.`;
  }
}

export const ESTIMATE_NOTE = "Estimate at standard paid-tier API prices, not your actual bill.";
