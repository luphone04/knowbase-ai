// What the user sees for a failed request. The API's messages are written for
// clients, so they are shown as they are, except where more guidance helps.

import { ApiError, type FieldError } from "./api";

const AI_MESSAGES: Record<string, string> = {
  ai_quota:
    "Gemini's free-tier limit has been reached. Nothing was saved. Try again later.",
  ai_unavailable: "Gemini is unavailable right now. Nothing was saved. Try again in a moment.",
  // Not naming the variable: the build check refuses any mention of it in the bundle.
  ai_not_configured:
    "The backend has no Gemini API key, so AI features are unavailable. Add the key to the backend .env.",
  ai_incomplete: "Gemini returned an incomplete answer. Nothing was saved. Try again.",
  ai_error: "Gemini returned an invalid response. Nothing was saved. Try again.",
};

export function isAiError(error: unknown): boolean {
  return error instanceof ApiError && error.code in AI_MESSAGES;
}

function waitText(seconds: number): string {
  if (seconds < 90) return `${seconds} seconds`;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 90) return `${minutes} minutes`;
  return `${Math.ceil(minutes / 60)} hours`;
}

export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong. Try again.";
  if (error.code in AI_MESSAGES) return AI_MESSAGES[error.code];
  switch (error.code) {
    case "network_error":
      return "Could not reach the Knowbase API. Check that the backend is running.";
    case "rate_limited":
      return error.retryAfterSeconds
        ? `You've reached a usage limit. Try again in ${waitText(error.retryAfterSeconds)}.`
        : "You've reached a usage limit. Try again later.";
    case "origin_not_allowed":
      return "The API refused this page's origin. Open the app at the address set in FRONTEND_ORIGIN.";
    case "not_authenticated":
      return "Your session has ended. Sign in again.";
  }
  if (error.status >= 500) return "The server hit an unexpected error. Try again.";
  return error.message;
}

/** Validation messages per request field ("email", "password", ...) from a 422. */
export function fieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError) || error.code !== "validation_error") return {};
  const fields: Record<string, string> = {};
  for (const detail of error.details as FieldError[]) {
    const field = detail.loc[detail.loc.length - 1];
    if (typeof field === "string" && !(field in fields)) fields[field] = detail.msg;
  }
  return fields;
}
