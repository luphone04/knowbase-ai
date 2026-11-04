import { describe, expect, it } from "vitest";

import { citationIdFromHref, isSafeExternalUrl, linkCitations, pageLabel } from "./citations";

const cited = [{ id: "S1" }, { id: "S2" }];

describe("linkCitations", () => {
  it("links a single marker", () => {
    expect(linkCitations("Returns take 14 days [S1].", cited)).toBe(
      "Returns take 14 days [S1](#cite-S1).",
    );
  });

  it("links each id in a group the backend wrote", () => {
    expect(linkCitations("Both apply [S1, S2].", cited)).toBe(
      "Both apply [S1](#cite-S1) [S2](#cite-S2).",
    );
  });

  it("leaves markers without a citation as text", () => {
    expect(linkCitations("Unknown [S9].", cited)).toBe("Unknown [S9].");
    expect(linkCitations("Mixed [S1, S9].", cited)).toBe("Mixed [S1](#cite-S1) S9.");
  });

  it("does not touch other brackets or an answer without citations", () => {
    expect(linkCitations("See [the policy] and [S1x].", cited)).toBe(
      "See [the policy] and [S1x].",
    );
    expect(linkCitations("No markers here.", [])).toBe("No markers here.");
  });
});

describe("citationIdFromHref", () => {
  it("reads only citation links", () => {
    expect(citationIdFromHref("#cite-S2")).toBe("S2");
    expect(citationIdFromHref("#cite-javascript:alert(1)")).toBeNull();
    expect(citationIdFromHref("https://example.com")).toBeNull();
    expect(citationIdFromHref(undefined)).toBeNull();
  });
});

describe("isSafeExternalUrl", () => {
  it("allows only absolute http(s) links", () => {
    expect(isSafeExternalUrl("https://example.com/returns")).toBe(true);
    expect(isSafeExternalUrl("http://example.com")).toBe(true);
    expect(isSafeExternalUrl("javascript:alert(1)")).toBe(false);
    expect(isSafeExternalUrl("data:text/html,hi")).toBe(false);
    expect(isSafeExternalUrl("/relative")).toBe(false);
    expect(isSafeExternalUrl(undefined)).toBe(false);
  });
});

describe("pageLabel", () => {
  it("formats single pages and ranges", () => {
    expect(pageLabel({ page_start: 3, page_end: 3 })).toBe("Page 3");
    expect(pageLabel({ page_start: 3, page_end: null })).toBe("Page 3");
    expect(pageLabel({ page_start: 3, page_end: 4 })).toBe("Pages 3–4");
    expect(pageLabel({ page_start: null, page_end: null })).toBeNull();
  });
});
