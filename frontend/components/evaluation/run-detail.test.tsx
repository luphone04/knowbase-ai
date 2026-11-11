// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { CollapsibleCard } from "@/components/collapsible-card";

import { RunDetail } from "./run-detail";
import { runDetail } from "./test-fixtures";

afterEach(cleanup);

const SECTIONS = [
  "Overview",
  "Retrieval comparison",
  "Answer evaluation",
  "API Usage & Estimated Cost",
  "Per-question results",
];

function toggle(name: string): HTMLElement {
  return screen.getByRole("button", { name });
}

function region(name: string): HTMLElement {
  return document.getElementById(toggle(name).getAttribute("aria-controls")!)!;
}

describe("CollapsibleCard", () => {
  it("is a disclosure: a heading with a button that controls a labelled region", () => {
    render(
      <CollapsibleCard title="Section" summary="Key result" badges={<span>Done</span>} description="Why">
        <p>Body</p>
      </CollapsibleCard>,
    );
    const button = toggle("Section");
    expect(button.getAttribute("type")).toBe("button"); // native: Enter and Space toggle it
    expect(button.closest("h2")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 2, name: "Section" })).toBeTruthy();
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("region", { name: "Section" }).id).toBe(button.getAttribute("aria-controls"));
    // Named by its title alone; badges and summary describe it.
    expect(button.getAttribute("aria-describedby")!.split(" ").map((id) => document.getElementById(id)!.textContent)).toEqual([
      "Done",
      "Key result",
    ]);
    expect(screen.getByText("Why")).toBeTruthy();
  });

  it("collapses and expands on click, keeping the summary visible", () => {
    render(
      <CollapsibleCard title="Section" summary="Key result" defaultOpen={false}>
        <p>Body</p>
      </CollapsibleCard>,
    );
    const button = toggle("Section");
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(region("Section").hidden).toBe(true);
    expect(screen.queryByRole("region")).toBeNull(); // hidden from assistive technology
    expect(screen.getByText("Key result")).toBeTruthy();

    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(region("Section").hidden).toBe(false);
    expect(screen.getByText("Body")).toBeTruthy();

    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(region("Section").hidden).toBe(true);
  });
});

describe("RunDetail sections", () => {
  it("starts with only the overview open", () => {
    render(<RunDetail run={runDetail()} />);
    expect(SECTIONS.map((name) => toggle(name).getAttribute("aria-expanded"))).toEqual([
      "true",
      "false",
      "false",
      "false",
      "false",
    ]);
    expect(region("Overview").hidden).toBe(false);
    expect(screen.queryByRole("table", { name: "Cost breakdown" })).toBeNull();
    for (const name of SECTIONS.slice(1)) expect(region(name).hidden).toBe(true);
  });

  it("shows each section's key results while collapsed", () => {
    render(<RunDetail run={runDetail()} />);
    expect(screen.getByText("2 of 2 questions evaluated · northstar-demo v1 · gemini-3.5-flash-lite")).toBeTruthy();
    expect(within(toggle("Overview")).getByText("Completed")).toBeTruthy();
    expect(
      screen.getByText(
        "MRR: Vector only 0.90 · Hybrid: keyword AND, simple (production) 0.00 · Hybrid: keyword OR, simple —",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText("Hybrid: abstention accuracy 0.92 · citation precision 0.97 · 1 provider error(s)"),
    ).toBeTruthy();
    expect(screen.getByText("$0.000503 estimated at paid-tier API pricing · 3 Gemini requests")).toBeTruthy();
    expect(screen.getByText("2 of 2 questions evaluated · 1 with errors")).toBeTruthy();
  });

  it("opens several sections at once and keeps every metric", () => {
    render(<RunDetail run={runDetail()} />);
    fireEvent.click(toggle("Retrieval comparison"));
    fireEvent.click(toggle("API Usage & Estimated Cost"));
    expect(toggle("Overview").getAttribute("aria-expanded")).toBe("true");
    expect(toggle("Retrieval comparison").getAttribute("aria-expanded")).toBe("true");
    expect(toggle("API Usage & Estimated Cost").getAttribute("aria-expanded")).toBe("true");
    expect(toggle("Answer evaluation").getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByRole("table", { name: "Cost breakdown" })).toBeTruthy();
    expect(within(screen.getByTestId("retrieval-vector")).getAllByRole("cell")).toHaveLength(7);
    expect(screen.getByText("Estimated cost at standard paid-tier API pricing.")).toBeTruthy();

    fireEvent.click(toggle("Answer evaluation"));
    expect(screen.getByRole("table", { name: "Hybrid decisions" })).toBeTruthy();
  });

  it("marks an incomplete estimate in the collapsed usage summary", () => {
    const run = runDetail();
    run.summary.usage!.total = { ...run.summary.usage!.total, usage_complete: false };
    render(<RunDetail run={run} />);
    expect(
      screen.getByText("≥ $0.000503 estimated at paid-tier API pricing (incomplete) · 3 Gemini requests"),
    ).toBeTruthy();
  });

  it("says when answers or usage weren't measured", () => {
    const run = runDetail({ answer_modes: [] });
    run.summary.answers = {};
    delete run.summary.usage;
    render(<RunDetail run={run} />);
    expect(screen.getByText("Not measured: this run evaluated retrieval only.")).toBeTruthy();
    expect(screen.getByText("Not measured for this run.")).toBeTruthy();
  });

  it("shows only the overview and questions before any question is done", () => {
    render(<RunDetail run={runDetail({ status: "running", questions_done: 0 })} />);
    expect(toggle("Overview")).toBeTruthy();
    expect(toggle("Per-question results")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retrieval comparison" })).toBeNull();
    expect(screen.getByRole("progressbar", { name: "Questions evaluated" })).toBeTruthy();
  });
});

describe("per-question items", () => {
  function question(key: string): HTMLDetailsElement {
    return screen.getByTestId(`question-${key}`) as HTMLDetailsElement;
  }

  it("start collapsed with their title and status visible, and open independently", () => {
    render(<RunDetail run={runDetail()} />);
    fireEvent.click(toggle("Per-question results"));
    const q01 = question("q01");
    const u01 = question("u01");
    expect([q01.open, u01.open]).toEqual([false, false]);
    const summary = q01.querySelector("summary")!;
    expect(within(summary).getByText("How many days do I have to return an unused item?")).toBeTruthy();
    expect(within(summary).getByText("Answered")).toBeTruthy();
    expect(within(u01.querySelector("summary")!).getByText("Error")).toBeTruthy();
    expect(summary.querySelector("svg[aria-hidden]")).toBeTruthy(); // the chevron

    q01.open = true;
    u01.open = true;
    expect([q01.open, u01.open]).toEqual([true, true]);
    expect(within(q01).getByTestId("answer-hybrid")).toBeTruthy();
    expect(within(q01).getByTestId("usage-q01")).toBeTruthy();
    expect(within(q01).getByText(/Citations:/)).toBeTruthy();
  });

  it("stay open when their section is collapsed and reopened", () => {
    render(<RunDetail run={runDetail()} />);
    fireEvent.click(toggle("Per-question results"));
    question("q01").open = true;
    fireEvent.click(toggle("Per-question results"));
    expect(region("Per-question results").hidden).toBe(true);
    fireEvent.click(toggle("Per-question results"));
    expect(question("q01").open).toBe(true);
    expect(question("u01").open).toBe(false);
  });
});
