"use client";

import { useQuery } from "@tanstack/react-query";
import { Coins, Info, LoaderCircle, MoreHorizontal } from "lucide-react";
import { DropdownMenu } from "radix-ui";
import { useId, useState } from "react";

import { ErrorAlert } from "@/components/error-alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { ESTIMATE_NOTE, chatUsageNote, costText, tokensText } from "@/lib/chat-usage";
import { conversationUsageKey } from "@/lib/queries";
import type { ChatUsageGroup, ChatUsageIssue, MessageUsage } from "@/lib/types";

// --- one reply ---------------------------------------------------------------------------------

/** The ⓘ under a reply: shows or hides that reply's usage and estimated cost. */
export function ReplyUsageButton({
  open,
  onToggle,
  panelId,
}: {
  open: boolean;
  onToggle: () => void;
  panelId: string;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={panelId}
      aria-label={open ? "Hide usage and estimated cost" : "Show usage and estimated cost"}
      title="Usage and estimated cost"
      onClick={onToggle}
      className="-my-1 inline-flex size-6 shrink-0 items-center justify-center rounded-full text-muted-foreground outline-none hover:text-foreground focus-visible:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 aria-expanded:text-foreground"
    >
      <Info aria-hidden className="size-3.5" />
    </button>
  );
}

/** A reply's tokens and estimated cost; "Not available" for a reply saved without usage. */
export function ReplyUsagePanel({ id, usage }: { id: string; usage: MessageUsage | null | undefined }) {
  return (
    <section
      id={id}
      aria-label="Usage and estimated cost"
      className="mt-2 rounded-md border bg-background p-2.5 text-xs text-foreground"
      data-testid="reply-usage"
    >
      {usage ? (
        <>
          <UsageGrid
            group={usage.total}
            rows={[["Embedding requests", String(usage.embedding.requests)]]}
          />
          <Notes issues={usage.issues} />
        </>
      ) : (
        <p className="text-muted-foreground">
          Not available: this reply was saved before usage was recorded.
        </p>
      )}
    </section>
  );
}

/** Owns the open state of one reply's usage panel. */
export function useReplyUsage() {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  return { open, panelId, toggle: () => setOpen((value) => !value) };
}

// --- the whole conversation ----------------------------------------------------------------

/** The ⋯ menu in the chat header, with "Conversation usage". */
export function ConversationMenu({ conversationId }: { conversationId: string }) {
  const [usageOpen, setUsageOpen] = useState(false);
  return (
    <>
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <Button variant="ghost" size="icon" aria-label="Conversation options">
            <MoreHorizontal />
          </Button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="end"
            sideOffset={4}
            collisionPadding={8}
            className="z-50 min-w-48 rounded-md border bg-popover p-1 text-sm text-popover-foreground shadow-md"
          >
            <DropdownMenu.Item
              onSelect={() => setUsageOpen(true)}
              className="flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 outline-none select-none data-highlighted:bg-muted"
            >
              <Coins aria-hidden className="size-4 text-muted-foreground" />
              Conversation usage
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
      <Dialog open={usageOpen} onOpenChange={setUsageOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Conversation usage</DialogTitle>
            <DialogDescription>Gemini tokens and estimated cost of this conversation.</DialogDescription>
          </DialogHeader>
          {usageOpen ? <ConversationUsageBody conversationId={conversationId} /> : null}
        </DialogContent>
      </Dialog>
    </>
  );
}

function ConversationUsageBody({ conversationId }: { conversationId: string }) {
  const usage = useQuery({
    queryKey: conversationUsageKey(conversationId),
    queryFn: () => api.conversations.usage(conversationId),
  });
  if (usage.isPending) {
    return (
      <p role="status" className="flex items-center gap-2 text-muted-foreground">
        <LoaderCircle aria-hidden className="size-4 animate-spin" />
        Adding up usage…
      </p>
    );
  }
  if (usage.isError) return <ErrorAlert error={usage.error} title="Couldn't load the usage" />;
  const data = usage.data;
  const rows: [string, string][] = [
    ["Embedding requests", String(data.embedding.requests)],
    ["Answer requests", String(data.generation.requests)],
    [
      "AI replies included",
      data.replies_with_usage === data.replies
        ? String(data.replies)
        : `${data.replies_with_usage} of ${data.replies}`,
    ],
  ];
  if (data.failed_questions) rows.push(["Failed questions included", String(data.failed_questions)]);
  return (
    <div className="grid gap-3 text-sm" data-testid="conversation-usage">
      <UsageGrid group={data.total} rows={rows} wide />
      {data.replies_with_usage < data.replies ? (
        <p className="text-xs text-muted-foreground">
          {data.replies - data.replies_with_usage} earlier repl
          {data.replies - data.replies_with_usage === 1 ? "y was" : "ies were"} saved before usage
          was recorded and aren&apos;t counted.
        </p>
      ) : null}
      <Notes issues={data.issues} />
      {data.pricing ? (
        <p className="text-xs text-muted-foreground">
          Prices: {data.pricing.tier_label}, checked {data.pricing.verified_on}.
        </p>
      ) : null}
    </div>
  );
}

// --- shared ----------------------------------------------------------------------------------

function UsageGrid({
  group,
  rows,
  wide,
}: {
  group: ChatUsageGroup;
  rows: [string, string][];
  wide?: boolean;
}) {
  const fields: [string, string][] = [
    ["Estimated API cost", costText(group)],
    ["Input tokens", tokensText(group.input_tokens)],
    ["Cached input tokens", tokensText(group.cached_input_tokens)],
    ["Output tokens", tokensText(group.output_tokens)],
    ["Thinking tokens", tokensText(group.thinking_tokens)],
    ["Total reported tokens", tokensText(group.total_tokens)],
    ...rows,
  ];
  return (
    <dl className={wide ? "grid grid-cols-2 gap-x-4 gap-y-2" : "grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3"}>
      {fields.map(([label, value], index) => (
        <div key={label} className="min-w-0">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className={index === 0 ? "font-semibold tabular-nums" : "tabular-nums"}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Notes({ issues }: { issues: ChatUsageIssue[] }) {
  return (
    <ul className="mt-2 grid gap-0.5 text-[11px] leading-snug text-muted-foreground">
      {issues.map((issue, index) => (
        <li key={index}>{chatUsageNote(issue)}</li>
      ))}
      <li>{ESTIMATE_NOTE}</li>
    </ul>
  );
}
