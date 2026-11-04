"use client";

import { useQuery } from "@tanstack/react-query";
import { MessageSquarePlus } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { ErrorAlert } from "@/components/error-alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { conversationsKey } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** The caller's conversations, newest activity first (the API returns at most 100). */
export function ConversationList({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const conversations = useQuery({ queryKey: conversationsKey, queryFn: api.conversations.list });

  return (
    <div className="flex flex-col gap-3">
      <Button variant="outline" asChild>
        <Link href="/chat" onClick={onNavigate}>
          <MessageSquarePlus />
          New chat
        </Link>
      </Button>
      {conversations.isPending ? (
        <div className="grid gap-2" role="status" aria-label="Loading conversations">
          <Skeleton className="h-9" />
          <Skeleton className="h-9" />
        </div>
      ) : conversations.isError ? (
        <ErrorAlert error={conversations.error} title="Couldn't load conversations" />
      ) : conversations.data.length === 0 ? (
        <p className="px-1 text-sm text-muted-foreground">No conversations yet.</p>
      ) : (
        <nav aria-label="Conversations">
          <ul className="flex min-w-0 flex-col gap-0.5">
            {conversations.data.map((conversation) => {
              const href = `/chat/${conversation.id}`;
              const active = pathname === href;
              return (
                <li key={conversation.id}>
                  <Link
                    href={href}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "block rounded-md px-2.5 py-1.5 hover:bg-muted",
                      active && "bg-muted",
                    )}
                  >
                    <span className="block truncate text-sm font-medium" title={conversation.title}>
                      {conversation.title}
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      {formatDateTime(conversation.updated_at)}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
      )}
    </div>
  );
}
