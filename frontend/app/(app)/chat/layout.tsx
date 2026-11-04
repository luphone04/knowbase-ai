import { ConversationList } from "@/components/chat/conversation-list";

/** Conversations beside the thread on wide screens; on narrow ones the thread opens them in a panel. */
export default function ChatLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-1">
      <aside className="hidden w-64 shrink-0 border-r p-4 md:block">
        <div className="sticky top-4">
          <ConversationList />
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">{children}</div>
    </div>
  );
}
