// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { RetrievalComparison } from "@/components/evaluation/run-summary";
import { runDetail } from "@/components/evaluation/test-fixtures";
import { InfoTip } from "@/components/info-tip";

beforeAll(() => {
  // Radix measures the tooltip to position it; jsdom has no ResizeObserver.
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

afterEach(cleanup);

const HELP = {
  "HitRate@5":
    "How often at least one correct document appears in the top 5 results. Higher is better; 1.00 means 100% of questions.",
  "Recall@5":
    "How many of the expected documents were found in the top 5 results. Higher is better; 1.00 means all expected documents were retrieved.",
  MRR: "Mean Reciprocal Rank. Measures how highly the first correct document is ranked. Rank #1 = 1.00, rank #2 = 0.50. Higher is better.",
  "No keyword match":
    "Number of questions where keyword search found zero matches. The vector search may still find relevant documents. Lower generally means broader keyword coverage, not necessarily better accuracy.",
};

const tooltip = () => screen.queryByRole("tooltip");

function renderTip() {
  render(
    <>
      <InfoTip label="MRR">{HELP.MRR}</InfoTip>
      <button type="button">Elsewhere</button>
    </>,
  );
  return screen.getByRole("button", { name: "About MRR" });
}

describe("InfoTip", () => {
  it("names the button and always describes it, with the icon hidden", () => {
    const button = renderTip();
    expect(button.getAttribute("type")).toBe("button");
    const described = document.getElementById(button.getAttribute("aria-describedby")!)!;
    expect(described.textContent).toBe(HELP.MRR);
    expect(button.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
    expect(tooltip()).toBeNull();
  });

  it("opens on keyboard focus and closes on Escape or blur", async () => {
    const button = renderTip();
    act(() => button.focus());
    expect(tooltip()!.textContent).toBe(HELP.MRR);
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(tooltip()).toBeNull());

    act(() => button.blur());
    act(() => button.focus());
    expect(tooltip()).toBeTruthy();
    act(() => screen.getByRole("button", { name: "Elsewhere" }).focus());
    await waitFor(() => expect(tooltip()).toBeNull());
  });

  it("toggles with Enter or Space (a keyboard click)", async () => {
    const button = renderTip();
    act(() => button.focus());
    fireEvent.click(button, { detail: 0 });
    await waitFor(() => expect(tooltip()).toBeNull());
    fireEvent.click(button, { detail: 0 });
    expect(tooltip()).toBeTruthy();
  });

  it("opens on mouse hover and closes when the pointer leaves", async () => {
    const button = renderTip();
    fireEvent.pointerMove(button, { pointerType: "mouse" });
    await waitFor(() => expect(tooltip()!.textContent).toBe(HELP.MRR));
    fireEvent.pointerLeave(button, { pointerType: "mouse", clientX: 0, clientY: 0 });
    fireEvent.pointerMove(document.body, { pointerType: "mouse", clientX: 400, clientY: 400 });
    await waitFor(() => expect(tooltip()).toBeNull());
  });

  it("toggles on tap, for touch screens without hover", async () => {
    const button = renderTip();
    const tap = () => {
      fireEvent.pointerDown(button, { pointerType: "touch" });
      fireEvent.pointerUp(button, { pointerType: "touch" });
      fireEvent.click(button, { detail: 1 });
    };
    fireEvent.pointerMove(button, { pointerType: "touch" }); // touch never opens on "hover"
    expect(tooltip()).toBeNull();
    tap();
    expect(tooltip()!.textContent).toBe(HELP.MRR);
    tap();
    await waitFor(() => expect(tooltip()).toBeNull());
    tap();
    expect(tooltip()).toBeTruthy();
    await act(() => new Promise((resolve) => setTimeout(resolve, 0))); // Radix listens a tick later
    // A tap elsewhere (Radix completes a touch dismissal on the click that follows).
    fireEvent.pointerDown(document.body, { pointerType: "touch" });
    fireEvent.click(document.body);
    await waitFor(() => expect(tooltip()).toBeNull());
  });
});

describe("Retrieval comparison column help", () => {
  it("explains HitRate@5, Recall@5, MRR, and No keyword match, and only those", () => {
    render(<RetrievalComparison run={runDetail()} />);
    const headers = screen.getAllByRole("columnheader");
    const helped = headers.flatMap((h) => within(h).queryAllByRole("button"));
    expect(helped.map((b) => b.getAttribute("aria-label"))).toEqual(
      Object.keys(HELP).map((name) => `About ${name}`),
    );
    for (const [name, text] of Object.entries(HELP)) {
      const button = screen.getByRole("button", { name: `About ${name}` });
      expect(document.getElementById(button.getAttribute("aria-describedby")!)!.textContent).toBe(text);
    }
    expect(within(headers.find((h) => h.textContent === "Questions")!).queryByRole("button")).toBeNull();
  });

  it("shows the tooltip outside the scrolling table, so it isn't clipped", () => {
    render(<RetrievalComparison run={runDetail()} />);
    act(() => screen.getByRole("button", { name: "About MRR" }).focus());
    const shown = tooltip()!;
    expect(shown.textContent).toBe(HELP.MRR);
    expect(screen.getByRole("table").contains(shown)).toBe(false);
    expect(shown.closest("[data-radix-popper-content-wrapper]")!.parentElement).toBe(document.body);
  });

  it("keeps the table's values and layout", () => {
    render(<RetrievalComparison run={runDetail()} />);
    const cells = within(screen.getByTestId("retrieval-vector")).getAllByRole("cell");
    expect(cells).toHaveLength(7);
    expect(cells.slice(1, 4).map((c) => c.textContent)).toEqual(["1.00", "0.95", "0.90"]);
  });
});
