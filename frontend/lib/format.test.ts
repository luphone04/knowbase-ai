import { describe, expect, it } from "vitest";

import { formatBytes, pageCount, uploadProblem } from "./format";

describe("formatBytes", () => {
  it("formats sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(220237)).toBe("215.1 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
    expect(formatBytes(null)).toBe("—");
  });
});

describe("pageCount", () => {
  it("reads the parser's page count only when it is an integer", () => {
    expect(pageCount({ page_count: 2 })).toBe(2);
    expect(pageCount({ page_count: "2" })).toBeNull();
    expect(pageCount({})).toBeNull();
  });
});

describe("uploadProblem", () => {
  it("accepts the supported types within the limit", () => {
    for (const name of ["a.pdf", "b.DOCX", "c.txt", "d.md"]) {
      expect(uploadProblem({ name, size: 100 }, 10)).toBeNull();
    }
  });

  it("rejects other types, empty files, and files over the limit", () => {
    expect(uploadProblem({ name: "a.doc", size: 100 }, 10)).toContain("Only PDF");
    expect(uploadProblem({ name: "a.pdf", size: 0 }, 10)).toContain("empty");
    expect(uploadProblem({ name: "a.pdf", size: 10 * 1024 * 1024 + 1 }, 10)).toContain("10 MB");
  });
});
