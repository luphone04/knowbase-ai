// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { ChatThread } from "@/components/chat/chat-thread";
import { AssistantMessage } from "@/components/chat/message-item";
import type {
  ChatUsageGroup,
  ConversationDetail,
  ConversationUsage,
  Message,
  MessageUsage,
} from "@/lib/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

beforeAll(() => {
  Element.prototype.scrollIntoView ??= () => {};
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function group(overrides: Partial<ChatUsageGroup> = {}): ChatUsageGroup {
  return {
    requests: 1,
    requests_without_usage: 0,
    unmeasured_attempts: 0,
    usage_complete: true,
    input_tokens: 1000,
    cached_input_tokens: 400,
    output_tokens: 50,
    thinking_tokens: 30,
    total_tokens: 1080,
    cost_usd: 0.000392,
    cost_complete: true,
    ...overrides,
  };
}

const UNREPORTED_EMBEDDING = group({
  requests_without_usage: 1,
  usage_complete: false,
  input_tokens: null,
  cached_input_tokens: null,
  output_tokens: null,
  thinking_tokens: null,
  total_tokens: null,
  cost_usd: null,
  cost_complete: false,
});

function messageUsage(overrides: Partial<MessageUsage> = {}): MessageUsage {
  return {
    pricing: {
      tier_label: "Gemini Developer API, Paid tier, Standard",
      verified_on: "2026-11-11",
      source_url: "https://aistudio.google.com/docs/pricing",
    },
    embedding: { ...UNREPORTED_EMBEDDING, model: "gemini-embedding-001" },
    generation: { ...group(), model: "gemini-3.5-flash-lite" },
    total: group({ requests: 2, requests_without_usage: 1, usage_complete: false, cost_complete: false }),
    issues: [{ code: "usage_missing", kind: "embedding", requests: 1 }],
    ...overrides,
  };
}

function reply(overrides: Partial<Message> = {}): Message {
  return {
    id: "m2",
    position: 2,
    role: "assistant",
    content: "Laptops can be returned within 14 days [S1].",
    outcome: "answered",
    abstain_reason: null,
    citations: [],
    token_usage: {},
    usage: messageUsage(),
    latency_ms: 1500,
    metadata: { retrieval_mode: "hybrid", excerpts: 3 },
    created_at: "2026-11-11T10:00:00Z",
    ...overrides,
  };
}

function field(container: HTMLElement, label: string): string {
  return within(container).getByText(label).closest("div")!.querySelector("dd")!.textContent!;
}

describe("reply usage", () => {
  it("is hidden until the ⓘ is pressed, and hides again", () => {
    render(<AssistantMessage message={reply()} onCite={() => {}} />);
    const button = screen.getByRole("button", { name: "Show usage and estimated cost" });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("reply-usage")).toBeNull();
    expect(screen.queryByText(/\$/)).toBeNull(); // no price in the chat by default
    expect(screen.getByText("Hybrid search · 3 excerpts · 1.5 s")).toBeTruthy(); // unchanged

    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(button.getAttribute("aria-label")).toBe("Hide usage and estimated cost");
    const panel = screen.getByRole("region", { name: "Usage and estimated cost" });
    expect(panel.id).toBe(button.getAttribute("aria-controls"));

    fireEvent.click(button);
    expect(screen.queryByTestId("reply-usage")).toBeNull();
  });

  it("shows tokens, a lower-bound cost, and why it is a lower bound", () => {
    render(<AssistantMessage message={reply()} onCite={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Show usage and estimated cost" }));
    const panel = screen.getByTestId("reply-usage");
    expect(field(panel, "Estimated API cost")).toBe("≥ $0.000392");
    expect(field(panel, "Input tokens")).toBe("1,000");
    expect(field(panel, "Cached input tokens")).toBe("400");
    expect(field(panel, "Output tokens")).toBe("50");
    expect(field(panel, "Thinking tokens")).toBe("30");
    expect(field(panel, "Total reported tokens")).toBe("1,080");
    expect(field(panel, "Embedding requests")).toBe("1");
    expect(within(panel).getByText(/Gemini doesn't report tokens for search embeddings/)).toBeTruthy();
    expect(within(panel).getByText(/not your actual bill/)).toBeTruthy();
  });

  it("says not reported or not available instead of zero", () => {
    const usage = messageUsage({
      generation: { ...UNREPORTED_EMBEDDING, model: "gemini-3.5-flash-lite" },
      total: { ...UNREPORTED_EMBEDDING, requests: 2, requests_without_usage: 2 },
      issues: [
        { code: "usage_missing", kind: "embedding", requests: 1 },
        { code: "usage_missing", kind: "generation", requests: 1 },
      ],
    });
    render(<AssistantMessage message={reply({ usage })} onCite={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Show usage and estimated cost" }));
    const panel = screen.getByTestId("reply-usage");
    expect(field(panel, "Estimated API cost")).toBe("Not available");
    expect(field(panel, "Input tokens")).toBe("Not reported");
    expect(field(panel, "Thinking tokens")).toBe("Not reported");
    expect(within(panel).queryByText("0")).toBeNull();
    expect(within(panel).getByText(/reported no token counts for 1 answer request/)).toBeTruthy();
  });

  it("discloses retried attempts", () => {
    const usage = messageUsage({ issues: [{ code: "unmeasured_attempts", kind: "generation", attempts: 2 }] });
    render(<AssistantMessage message={reply({ usage })} onCite={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Show usage and estimated cost" }));
    expect(screen.getByText("2 retried answer attempt(s) aren't measured or included.")).toBeTruthy();
  });

  it("is not available for a reply saved before usage was recorded", () => {
    render(<AssistantMessage message={reply({ usage: null })} onCite={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Show usage and estimated cost" }));
    expect(screen.getByText("Not available: this reply was saved before usage was recorded.")).toBeTruthy();
  });

  it("works under an abstention, and one that made no Gemini call costs $0.00", () => {
    const none = group({ requests: 0, cost_usd: 0, input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, thinking_tokens: 0, total_tokens: 0 });
    const usage = messageUsage({
      pricing: null,
      embedding: { ...none, model: null },
      generation: { ...none, model: null },
      total: none,
      issues: [],
    });
    render(
      <AssistantMessage
        message={reply({ outcome: "abstained", abstain_reason: "no_evidence", content: "Not found.", usage })}
        onCite={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Show usage and estimated cost" }));
    const panel = screen.getByTestId("reply-usage");
    expect(field(panel, "Estimated API cost")).toBe("$0.00");
    expect(field(panel, "Embedding requests")).toBe("0");
  });

  it("keeps each reply's panel independent", () => {
    render(
      <>
        <AssistantMessage message={reply({ id: "a" })} onCite={() => {}} />
        <AssistantMessage message={reply({ id: "b" })} onCite={() => {}} />
      </>,
    );
    const [first, second] = screen.getAllByRole("button", { name: "Show usage and estimated cost" });
    fireEvent.click(first);
    expect(screen.getAllByTestId("reply-usage")).toHaveLength(1);
    expect(second.getAttribute("aria-expanded")).toBe("false");
  });
});

// --- conversation usage ----------------------------------------------------------------------

function conversationUsage(overrides: Partial<ConversationUsage> = {}): ConversationUsage {
  return {
    conversation_id: "c1",
    replies: 3,
    replies_with_usage: 2,
    failed_questions: 1,
    pricing: messageUsage().pricing,
    embedding: { ...UNREPORTED_EMBEDDING, requests: 3, requests_without_usage: 3 },
    generation: group({ requests: 3, requests_without_usage: 1, usage_complete: false, input_tokens: 2000 }),
    total: group({
      requests: 6,
      requests_without_usage: 4,
      usage_complete: false,
      input_tokens: 2000,
      cached_input_tokens: 800,
      output_tokens: 100,
      thinking_tokens: 60,
      total_tokens: 2160,
      cost_usd: 0.000784,
      cost_complete: false,
    }),
    issues: [
      { code: "usage_missing", kind: "embedding", requests: 3 },
      { code: "usage_missing", kind: "generation", requests: 1 },
    ],
    ...overrides,
  };
}

function conversation(): ConversationDetail {
  return {
    id: "c1",
    title: "Laptop returns",
    created_at: "2026-11-11T10:00:00Z",
    updated_at: "2026-11-11T10:00:00Z",
    messages: [
      { ...reply({ id: "m1", position: 1, role: "user", outcome: null, content: "Can I return a laptop?", usage: null }) },
      reply(),
    ],
  };
}

function renderThread(responses: { usage: ConversationUsage[] }) {
  const usageQueue = [...responses.usage];
  const fetch = vi.fn(async (url: string) => {
    if (url.endsWith("/limits")) {
      return new Response(
        JSON.stringify({
          chat: { limit: 30, remaining: 24 },
          retrieval: { limit: 100, remaining: 85 },
          resets_at: "2026-11-12T00:00:00Z",
        }),
      );
    }
    if (url.endsWith("/conversations/c1/usage")) {
      return new Response(JSON.stringify(usageQueue.shift() ?? responses.usage.at(-1)));
    }
    if (url.endsWith("/conversations/c1/messages")) {
      return new Response(
        JSON.stringify({
          conversation: { ...conversation(), messages: undefined },
          user_message: { ...reply({ id: "m3", position: 3, role: "user", outcome: null, usage: null }) },
          assistant_message: reply({ id: "m4", position: 4 }),
          retrieval: { mode: "hybrid", retrieval_query: "q", contextualized: false, trace_id: "t", results: 3 },
        }),
        { status: 201 },
      );
    }
    return new Response(JSON.stringify([]));
  });
  vi.stubGlobal("fetch", fetch);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ChatThread conversation={conversation()} />
    </QueryClientProvider>,
  );
  return fetch;
}

async function openConversationUsage() {
  const trigger = screen.getByRole("button", { name: "Conversation options" });
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: "mouse" });
  const item = await screen.findByRole("menuitem", { name: "Conversation usage" });
  fireEvent.click(item);
  return screen.findByRole("dialog", { name: "Conversation usage" });
}

describe("conversation usage", () => {
  it("lives behind the header menu, not in the header", async () => {
    renderThread({ usage: [conversationUsage()] });
    const header = screen.getByRole("banner");
    expect(within(header).queryByText(/\$|tokens/)).toBeNull();
    const dialog = await openConversationUsage();
    const body = await within(dialog).findByTestId("conversation-usage");
    expect(field(body, "Estimated API cost")).toBe("≥ $0.000784");
    expect(field(body, "Input tokens")).toBe("2,000");
    expect(field(body, "Cached input tokens")).toBe("800");
    expect(field(body, "Output tokens")).toBe("100");
    expect(field(body, "Thinking tokens")).toBe("60");
    expect(field(body, "Total reported tokens")).toBe("2,160");
    expect(field(body, "Embedding requests")).toBe("3");
    expect(field(body, "Answer requests")).toBe("3");
    expect(field(body, "AI replies included")).toBe("2 of 3");
    expect(field(body, "Failed questions included")).toBe("1");
    expect(within(body).getByText(/1 earlier reply was saved before usage was recorded/)).toBeTruthy();
    expect(within(body).getByText(/Gemini doesn't report tokens for search embeddings/)).toBeTruthy();
    expect(within(body).getByText(/checked 2026-11-11/)).toBeTruthy();
  });

  it("refreshes its totals after a new answer is saved", async () => {
    const later = conversationUsage({
      replies: 4,
      replies_with_usage: 3,
      total: { ...conversationUsage().total, cost_usd: 0.001176 },
    });
    const fetch = renderThread({ usage: [conversationUsage(), later] });
    let dialog = await openConversationUsage();
    await within(dialog).findByText("≥ $0.000784");
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "And a tent?" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
    });
    // The answer is saved (the page's query shows it; this thread's props are fixed here).
    await waitFor(() =>
      expect(fetch.mock.calls.some(([url]) => String(url).endsWith("/messages"))).toBe(true),
    );
    await waitFor(() => expect(screen.getByLabelText("Your question").hasAttribute("disabled")).toBe(false));

    dialog = await openConversationUsage();
    await within(dialog).findByText("≥ $0.00118");
    expect(within(dialog).getByText("3 of 4")).toBeTruthy();
    const usageCalls = fetch.mock.calls.filter(([url]) => String(url).endsWith("/usage"));
    expect(usageCalls).toHaveLength(2);
  });

  it("opens from the keyboard", async () => {
    renderThread({ usage: [conversationUsage()] });
    const trigger = screen.getByRole("button", { name: "Conversation options" });
    act(() => trigger.focus());
    fireEvent.keyDown(trigger, { key: "Enter" });
    const item = await screen.findByRole("menuitem", { name: "Conversation usage" });
    fireEvent.keyDown(item, { key: "Enter" });
    expect(await screen.findByRole("dialog", { name: "Conversation usage" })).toBeTruthy();
  });

  it("shows the API's error instead of totals", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ error: { code: "not_found", message: "Conversation not found." } }), {
          status: 404,
        }),
      ),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ChatThread conversation={conversation()} />
      </QueryClientProvider>,
    );
    const dialog = await openConversationUsage();
    expect(await within(dialog).findByText("Conversation not found.")).toBeTruthy();
  });
});
