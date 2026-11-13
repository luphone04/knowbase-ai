import { describe, expect, it } from "vitest";

import { allowanceHelp, allowanceText, exhaustedText, resetsIn } from "./limits";

const NOW = new Date("2026-11-11T18:48:00Z");
const MIDNIGHT = "2026-11-12T00:00:00Z";

describe("daily question allowance wording", () => {
  it("counts what's left of the reported limit", () => {
    expect(allowanceText({ limit: 30, remaining: 24 })).toBe("24 of 30 questions left today");
    expect(allowanceText({ limit: 1, remaining: 1 })).toBe("1 of 1 question left today");
  });

  it("says how long until the UTC reset", () => {
    expect(resetsIn(MIDNIGHT, NOW)).toBe("in 5 h 12 min");
    expect(resetsIn(MIDNIGHT, new Date("2026-11-11T23:52:00Z"))).toBe("in 8 min");
    expect(resetsIn(MIDNIGHT, new Date("2026-11-11T22:00:00Z"))).toBe("in 2 h");
    expect(resetsIn(MIDNIGHT, new Date("2026-11-11T23:59:59.500Z"))).toBe("in 1 min");
    expect(resetsIn(MIDNIGHT, new Date("2026-11-12T00:00:01Z"))).toBe("in under a minute");
  });

  it("explains an exhausted allowance, or one that is turned off", () => {
    expect(exhaustedText({ limit: 30, remaining: 0 }, MIDNIGHT, NOW)).toBe(
      "You've used all 30 questions for today. More are available after midnight UTC (in 5 h 12 min).",
    );
    expect(exhaustedText({ limit: 0, remaining: 0 }, MIDNIGHT, NOW)).toBe(
      "Questions aren't available on this server today.",
    );
  });

  it("explains the limit without mentioning searches", () => {
    const help = allowanceHelp({ limit: 30, remaining: 24 });
    expect(help).toMatch(/^You can ask 30 questions a day\. The count resets at midnight UTC\./);
    expect(help).not.toMatch(/search/i);
  });
});
