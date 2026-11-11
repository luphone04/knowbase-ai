import { describe, expect, it } from "vitest";

import { chatUsageNote, costText, tokensText } from "./chat-usage";

describe("chat usage formatting", () => {
  it("never shows an unknown value as zero", () => {
    expect(tokensText(null)).toBe("Not reported");
    expect(tokensText(undefined)).toBe("Not reported");
    expect(tokensText(0)).toBe("0");
    expect(tokensText(12345)).toBe("12,345");
  });

  it("shows a cost as exact, a lower bound, or not available", () => {
    expect(costText({ cost_usd: 0.000392, cost_complete: true })).toBe("$0.000392");
    expect(costText({ cost_usd: 0.000392, cost_complete: false })).toBe("≥ $0.000392");
    expect(costText({ cost_usd: 0, cost_complete: true })).toBe("$0.00");
    expect(costText({ cost_usd: null, cost_complete: false })).toBe("Not available");
  });

  it("explains each gap in a short sentence", () => {
    expect(chatUsageNote({ code: "usage_missing", kind: "embedding", requests: 2 })).toBe(
      "Gemini doesn't report tokens for search embeddings, so they aren't in the cost.",
    );
    expect(chatUsageNote({ code: "unmeasured_attempts", kind: "embedding", attempts: 1 })).toBe(
      "1 retried embedding attempt(s) aren't measured or included.",
    );
    expect(chatUsageNote({ code: "cost_unavailable", requests: 1 })).toMatch(/before prices were recorded/);
  });
});
