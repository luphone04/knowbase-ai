"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { History, LoaderCircle, MessageSquarePlus, SendHorizontal } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { CitationPanel } from "@/components/chat/citation-panel";
import { ConversationList } from "@/components/chat/conversation-list";
import { AssistantMessage, UserMessage } from "@/components/chat/message-item";
import { ConversationMenu } from "@/components/chat/usage-details";
import { ErrorAlert } from "@/components/error-alert";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api";
import { conversationKey, conversationUsageKey, conversationsKey, useSources } from "@/lib/queries";
import type { Citation, ConversationDetail, Message } from "@/lib/types";

const MAX_QUESTION_CHARS = 1000; // MessageIn.content in the API

/**
 * One conversation, or a new one when `conversation` is null: the first question
 * creates it. A question that fails (quota, Gemini unavailable, ...) is not saved
 * by the API, so it goes back into the box to try again.
 */
export function ChatThread({ conversation }: { conversation: ConversationDetail | null }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [citation, setCitation] = useState<Citation | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  // A new conversation is created once, and reused if its first question fails.
  const conversationId = useRef<string | null>(conversation?.id ?? null);
  const bottom = useRef<HTMLDivElement>(null);

  const ask = useMutation({
    mutationFn: async (content: string) => {
      conversationId.current ??= (await api.conversations.create()).id;
      return { id: conversationId.current, turn: await api.conversations.ask(conversationId.current, content) };
    },
    onSuccess: ({ id, turn }) => {
      queryClient.setQueryData<ConversationDetail>(conversationKey(id), (old) => ({
        ...turn.conversation,
        messages: [...(old?.messages ?? []), turn.user_message, turn.assistant_message],
      }));
      if (!conversation) router.replace(`/chat/${id}`);
    },
    onError: (_error, content) => setDraft(content),
    onSettled: () => {
      // A saved reply or a failed question both change the conversation's usage totals.
      if (conversationId.current) {
        void queryClient.invalidateQueries({ queryKey: conversationUsageKey(conversationId.current) });
      }
      return queryClient.invalidateQueries({ queryKey: conversationsKey, exact: true });
    },
  });

  const messages = conversation?.messages ?? [];
  const lastMessageId = messages.at(-1)?.id;
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [lastMessageId, ask.isPending, ask.isError]);

  function submit() {
    const content = draft.trim();
    if (!content || ask.isPending) return;
    setDraft("");
    ask.mutate(content);
  }

  return (
    <div className="flex flex-1 flex-col">
      <header className="flex items-center gap-2 border-b px-4 py-3 md:px-6">
        <Button
          variant="ghost"
          size="icon"
          className="md:hidden"
          onClick={() => setHistoryOpen(true)}
          aria-label="Conversations"
        >
          <History />
        </Button>
        <h1 className="min-w-0 flex-1 truncate text-base font-semibold">
          {conversation?.title ?? "New conversation"}
        </h1>
        {conversation ? (
          <>
            <Button variant="outline" size="sm" asChild>
              <Link href="/chat">
                <MessageSquarePlus />
                New chat
              </Link>
            </Button>
            <ConversationMenu conversationId={conversation.id} />
          </>
        ) : null}
      </header>

      <div className="flex flex-1 flex-col gap-4 px-4 py-6 md:px-6" aria-live="polite">
        {messages.length === 0 && !ask.isPending && !ask.isError ? <EmptyThread /> : null}
        {messages.map((message) => (
          <MessageView key={message.id} message={message} onCite={setCitation} />
        ))}
        {ask.isPending || ask.isError ? <UserMessage content={ask.variables ?? ""} /> : null}
        {ask.isPending ? (
          <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" aria-hidden />
            Searching your documents and writing an answer…
          </p>
        ) : null}
        {ask.isError ? (
          <div className="max-w-[85%]">
            <ErrorAlert error={ask.error} title="No answer this time" />
          </div>
        ) : null}
        {/* The margin keeps the last item clear of the sticky question box. */}
        <div ref={bottom} className="scroll-mb-32" />
      </div>

      <form
        className="sticky bottom-0 border-t bg-background px-4 py-3 md:px-6"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <div className="flex items-end gap-2">
          <Textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                submit();
              }
            }}
            placeholder={messages.length ? "Ask a follow-up…" : "Ask a question about your documents…"}
            aria-label="Your question"
            maxLength={MAX_QUESTION_CHARS}
            rows={2}
            disabled={ask.isPending}
            className="max-h-40 min-h-11 resize-none"
          />
          <Button type="submit" size="icon-lg" disabled={ask.isPending || !draft.trim()} aria-label="Send">
            <SendHorizontal />
          </Button>
        </div>
        <p className="mt-1.5 text-xs text-muted-foreground">
          Answers use only your indexed documents. Enter to send, Shift+Enter for a new line.
          {draft.length > MAX_QUESTION_CHARS - 100 ? ` ${draft.length}/${MAX_QUESTION_CHARS}` : ""}
        </p>
      </form>

      <CitationPanel citation={citation} onClose={() => setCitation(null)} />
      <Sheet open={historyOpen} onOpenChange={setHistoryOpen}>
        <SheetContent side="left" className="w-72">
          <SheetHeader>
            <SheetTitle>Conversations</SheetTitle>
          </SheetHeader>
          <div className="overflow-y-auto px-4 pb-4">
            <ConversationList onNavigate={() => setHistoryOpen(false)} />
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}

function MessageView({ message, onCite }: { message: Message; onCite: (c: Citation) => void }) {
  return message.role === "user" ? (
    <UserMessage content={message.content} />
  ) : (
    <AssistantMessage message={message} onCite={onCite} />
  );
}

function EmptyThread() {
  const sources = useSources();
  const ready = sources.data?.filter((s) => s.status === "ready").length ?? 0;
  return (
    <div className="m-auto max-w-sm text-center">
      <p className="font-medium">Ask about your documents</p>
      <p className="mt-1 text-sm text-muted-foreground">
        Answers cite the passages they use. When the documents don&apos;t cover a question, the
        assistant says so instead of guessing.
      </p>
      {sources.isSuccess && ready === 0 ? (
        <p className="mt-3 text-sm">
          No documents are ready yet.{" "}
          <Link href="/sources" className="font-medium underline underline-offset-4">
            Upload one first
          </Link>
          .
        </p>
      ) : null}
    </div>
  );
}
