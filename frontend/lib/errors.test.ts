import { describe, expect, it } from "vitest";

import { ApiError, toApiError } from "./api";
import { describeError, fieldErrors, isAiError } from "./errors";

function response(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

describe("toApiError", () => {
  it("reads the API's error shape and Retry-After", async () => {
    const error = await toApiError(
      response(429, { error: { code: "rate_limited", message: "Too many." } }, {
        "Retry-After": "120",
      }),
    );
    expect(error).toMatchObject({ status: 429, code: "rate_limited", retryAfterSeconds: 120 });
  });

  it("handles a body that is not the API's error shape", async () => {
    const error = await toApiError(new Response("<html>", { status: 502 }));
    expect(error).toMatchObject({ status: 502, code: "http_error" });
  });
});

describe("describeError", () => {
  it("explains provider failures without calling them answers", () => {
    for (const code of ["ai_quota", "ai_unavailable", "ai_incomplete", "ai_error"]) {
      const error = new ApiError(503, code, "provider text");
      expect(isAiError(error)).toBe(true);
      expect(describeError(error)).toContain("Nothing was saved");
    }
    expect(describeError(new ApiError(503, "ai_not_configured", "x"))).toContain(
      "GEMINI_API_KEY",
    );
  });

  it("shows the API's own message for client errors", () => {
    const error = new ApiError(422, "unsupported_type", "Only PDF, DOCX, TXT, and MD files.");
    expect(describeError(error)).toBe("Only PDF, DOCX, TXT, and MD files.");
    expect(isAiError(error)).toBe(false);
  });

  it("covers network failures, limits, and server errors", () => {
    expect(describeError(new ApiError(0, "network_error", ""))).toContain("Could not reach");
    expect(describeError(new ApiError(429, "rate_limited", "", [], 300))).toContain("5 minutes");
    expect(describeError(new ApiError(500, "internal_error", "Internal"))).toContain(
      "unexpected error",
    );
    expect(describeError(new Error("boom"))).toBe("Something went wrong. Try again.");
  });
});

describe("fieldErrors", () => {
  it("maps 422 details to request fields", () => {
    const error = new ApiError(422, "validation_error", "The request is invalid.", [
      { loc: ["body", "password"], msg: "String should have at least 12 characters", type: "x" },
      { loc: ["body", "email"], msg: "value is not a valid email address", type: "y" },
    ]);
    expect(fieldErrors(error)).toEqual({
      password: "String should have at least 12 characters",
      email: "value is not a valid email address",
    });
    expect(fieldErrors(new ApiError(401, "invalid_credentials", "x"))).toEqual({});
  });
});
