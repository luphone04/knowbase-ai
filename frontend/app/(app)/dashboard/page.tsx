"use client";

import { PageHeader } from "@/components/page-header";
import { useSession } from "@/lib/session";

export default function DashboardPage() {
  const { data: session } = useSession();
  if (!session) return null; // the layout renders pages only with a session

  return (
    <>
      <PageHeader
        title={session.workspace.name}
        description={`Signed in as ${session.user.email} (${session.workspace.role})`}
      />
    </>
  );
}
