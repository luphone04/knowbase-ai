// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import DashboardPage from "@/app/(app)/dashboard/page";
import { ChatThread } from "@/components/chat/chat-thread";
import { DailyAllowance } from "@/components/daily-allowance";
import type { ConversationDetail, DailyLimits } from "@/lib/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/session", () => ({
  useSession: () => ({
    data: {
      user: { id: "u1", email: "ada@example.com" },
      workspace: { id: "w1", name: "Northstar", role: "owner" },
    },
  }),
}));

beforeAll(() => {
  Element.prototype.scrollIntoView ??= () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function limits(overrides: Partial<DailyLimits> = {}): DailyLimits {
  return {
    chat: { limit: 30, remaining: 24 },
    resets_at: new Date(Date.now() + (5 * 60 + 12) * 60_000).toISOString(),
    ...overrides,
  };
}

function stubLimits(...responses: (DailyLimits | Response)[]) {
  const queue = [...responses];
  const fetch = vi.fn(async (url: string) => {
    if (url.endsWith("/limits")) {
      const next = queue.length > 1 ? queue.shift()! : queue[0];
      return next instanceof Response ? next : new Response(JSON.stringify(next));
    }
    return new Response(JSON.stringify([]));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function renderWithClient(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

describe("DailyAllowance", () => {
  it("shows what's left of the limit the API reports, never a fixed number", async () => {
    stubLimits(limits({ chat: { limit: 7, remaining: 3 } }));
    renderWithClient(<DailyAllowance />);
    expect(await screen.findByText("3 of 7 questions left today")).toBeTruthy();
    const help = screen.getByRole("button", { name: "About the daily question limit" });
    const described = document.getElementById(help.getAttribute("aria-describedby")!)!;
    expect(described.textContent).toMatch(/resets at midnight UTC/);
  });

  it("says when more questions are available once none are left", async () => {
    stubLimits(limits({ chat: { limit: 30, remaining: 0 } }));
    renderWithClient(<DailyAllowance />);
    const status = await screen.findByRole("status");
    expect(status.textContent).toMatch(
      /^You've used all 30 questions for today\. More are available after midnight UTC \(in 5 h 1[12] min\)\./,
    );
  });

  it("says so quietly when the allowance can't be loaded", async () => {
    stubLimits(new Response(JSON.stringify({ error: { code: "x", message: "Down." } }), { status: 500 }));
    renderWithClient(<DailyAllowance />);
    expect(await screen.findByText("Today's allowance is unavailable right now.")).toBeTruthy();
  });

  it("shows nothing but a screen-reader note while loading", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderWithClient(<DailyAllowance />);
    const placeholder = screen.getByTestId("allowance-chat");
    expect(placeholder.getAttribute("aria-busy")).toBe("true");
    expect(placeholder.textContent).toBe("Loading today's allowance…");
  });
});

describe("chat allowance", () => {
  it("sits under the question box and refreshes after a question", async () => {
    const conversation: ConversationDetail = {
      id: "c1",
      title: "Returns",
      created_at: "2026-11-11T10:00:00Z",
      updated_at: "2026-11-11T10:00:00Z",
      messages: [],
    };
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/limits")) {
        const asked = fetch.mock.calls.some(([u]) => String(u).endsWith("/messages"));
        return new Response(JSON.stringify(limits({ chat: { limit: 30, remaining: asked ? 23 : 24 } })));
      }
      if (url.endsWith("/messages")) {
        return new Response(JSON.stringify({ error: { code: "ai_unavailable", message: "Gemini is down." } }), {
          status: 503,
        });
      }
      return new Response(JSON.stringify([]));
    });
    vi.stubGlobal("fetch", fetch);
    renderWithClient(<ChatThread conversation={conversation} />);
    const allowance = await screen.findByText("24 of 30 questions left today");
    expect(allowance.closest("form")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "Laptops?" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
    });
    // A failed question may still have used a slot: the API's count is shown, not a guess.
    expect(await screen.findByText("23 of 30 questions left today")).toBeTruthy();
    await waitFor(() =>
      expect(fetch.mock.calls.filter(([u]) => String(u).endsWith("/limits")).length).toBe(2),
    );
  });
});

describe("dashboard allowance", () => {
  it("shows the questions left and no search allowance", async () => {
    // The API also reports the workspace's search allowance; the dashboard must not show it.
    const apiLimits = { ...limits(), retrieval: { limit: 100, remaining: 85 } };
    const aiStatus = {
      status: "configured",
      configured: true,
      message: "A Gemini API key is configured.",
      chat_model: "gemini-3.5-flash-lite",
      embedding_model: "gemini-embedding-001",
      embedding_dimensions: 768,
      last_error: null,
      last_success_at: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        new Response(
          JSON.stringify(url.endsWith("/limits") ? apiLimits : url.endsWith("/ai/status") ? aiStatus : []),
        ),
      ),
    );
    renderWithClient(<DashboardPage />);
    const section = (await screen.findByRole("heading", { name: "Today's allowance" })).closest("section")!;
    expect(await within(section).findByText("24 of 30 questions left today")).toBeTruthy();
    expect(screen.queryByText(/searches? left/)).toBeNull();
    expect(screen.queryByRole("button", { name: /search limit/ })).toBeNull();
    expect(screen.getAllByTestId("allowance-chat")).toHaveLength(1);
  });
});
