"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BookOpenText, FileText, LayoutDashboard, LogOut, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { Session } from "@/lib/types";
import { cn } from "@/lib/utils";

const NAV: { href: string; label: string; icon: LucideIcon }[] = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/sources", label: "Documents", icon: FileText },
];

/** Sidebar on wide screens; a top bar with the same links on narrow ones. */
export function AppShell({ session, children }: { session: Session; children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();
  const logout = useMutation({
    mutationFn: api.auth.logout,
    onSettled: () => {
      // The server revokes the token and clears the cookie; forget everything cached here too.
      queryClient.clear();
      router.replace("/login");
    },
  });

  const links = NAV.map(({ href, label, icon: Icon }) => {
    const active = pathname === href || pathname.startsWith(`${href}/`);
    return (
      <Link
        key={href}
        href={href}
        aria-current={active ? "page" : undefined}
        className={cn(
          "flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground",
          active && "bg-muted text-foreground",
        )}
      >
        <Icon className="size-4" aria-hidden />
        {label}
      </Link>
    );
  });

  return (
    <div className="flex min-h-svh flex-1 flex-col md:flex-row">
      <aside className="flex shrink-0 flex-col gap-3 border-b bg-muted/30 p-3 md:w-60 md:border-r md:border-b-0 md:p-4">
        <div className="flex items-center justify-between gap-2">
          <Link href="/dashboard" className="flex items-center gap-2 px-1 font-semibold tracking-tight">
            <BookOpenText className="size-5" aria-hidden />
            Knowbase AI
          </Link>
          <Button
            variant="ghost"
            size="icon"
            className="md:hidden"
            onClick={() => logout.mutate()}
            disabled={logout.isPending}
            aria-label="Log out"
          >
            <LogOut />
          </Button>
        </div>
        <nav aria-label="Main" className="flex gap-1 md:flex-col">
          {links}
        </nav>
        <div className="mt-auto hidden border-t pt-3 md:block">
          <p className="truncate text-sm font-medium" title={session.workspace.name}>
            {session.workspace.name}
          </p>
          <p className="truncate text-xs text-muted-foreground" title={session.user.email}>
            {session.user.email}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="mt-3 w-full"
            onClick={() => logout.mutate()}
            disabled={logout.isPending}
          >
            <LogOut />
            {logout.isPending ? "Logging out…" : "Log out"}
          </Button>
        </div>
      </aside>
      <main className="flex min-w-0 flex-1 flex-col">{children}</main>
    </div>
  );
}
