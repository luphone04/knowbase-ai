// Wording for the daily chat-question allowance (GET /limits). The numbers always come from the API.

import type { Allowance } from "./types";

function questions(count: number): string {
  return count === 1 ? "question" : "questions";
}

/** "24 of 30 questions left today". */
export function allowanceText(allowance: Allowance): string {
  return `${allowance.remaining} of ${allowance.limit} ${questions(allowance.limit)} left today`;
}

/** "in 5 h 12 min", "in 8 min", or "in under a minute". */
export function resetsIn(resetsAt: string, now: Date = new Date()): string {
  const minutes = Math.max(0, Math.ceil((new Date(resetsAt).getTime() - now.getTime()) / 60_000));
  if (minutes < 1) return "in under a minute";
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `in ${rest} min`;
  return rest ? `in ${hours} h ${rest} min` : `in ${hours} h`;
}

/** What to say once no questions are left (or none are allowed at all). */
export function exhaustedText(allowance: Allowance, resetsAt: string, now?: Date): string {
  if (allowance.limit === 0) return "Questions aren't available on this server today.";
  return `You've used all ${allowance.limit} ${questions(allowance.limit)} for today. More are available after midnight UTC (${resetsIn(resetsAt, now)}).`;
}

/** The ⓘ explanation. */
export function allowanceHelp(allowance: Allowance): string {
  return `You can ask ${allowance.limit} ${questions(allowance.limit)} a day. The count resets at midnight UTC. Questions that don't reach Gemini, such as when no documents are indexed, don't count.`;
}
