import { describe, expect, it } from "vitest";

import { HOME_PATH, loginPath, safeNextPath } from "./redirect";

describe("safeNextPath", () => {
  it("keeps same-origin paths", () => {
    expect(safeNextPath("/sources/123")).toBe("/sources/123");
    expect(safeNextPath("/chat?x=1")).toBe("/chat?x=1");
  });

  it("refuses other origins and odd values", () => {
    for (const value of ["https://evil.example", "//evil.example", "/\\evil.example", "chat", "", null]) {
      expect(safeNextPath(value)).toBe(HOME_PATH);
    }
  });
});

describe("loginPath", () => {
  it("carries the page to return to", () => {
    expect(loginPath("/sources/1")).toBe("/login?next=%2Fsources%2F1");
    expect(loginPath(HOME_PATH)).toBe("/login");
  });
});
