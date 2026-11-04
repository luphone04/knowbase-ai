"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";

import { ChatThread } from "@/components/chat/chat-thread";
import { ErrorAlert } from "@/components/error-alert";
import { PageLoading } from "@/components/page-loading";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { conversationKey } from "@/lib/queries";

export default function ConversationPage() {
  const { id } = useParams<{ id: string }>();
  const conversation = useQuery({
    queryKey: conversationKey(id),
    queryFn: () => api.conversations.get(id),
  });

  if (conversation.isPending) return <PageLoading label="Loading conversation…" />;
  if (conversation.isError) {
    const missing = conversation.error instanceof ApiError && conversation.error.status === 404;
    return (
      <div className="grid max-w-xl gap-4 p-4 md:p-6">
        {missing ? (
          <p className="text-sm text-muted-foreground">This conversation doesn&apos;t exist.</p>
        ) : (
          <ErrorAlert error={conversation.error} title="Couldn't load the conversation" />
        )}
        <Button variant="outline" size="sm" className="w-fit" asChild>
          <Link href="/chat">New chat</Link>
        </Button>
      </div>
    );
  }
  // Keyed by id so a different conversation starts with a fresh question box.
  return <ChatThread key={id} conversation={conversation.data} />;
}
