"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { AppShell } from "@/components/app-shell";
import { ErrorAlert } from "@/components/error-alert";
import { PageLoading } from "@/components/page-loading";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import { loginPath } from "@/lib/redirect";
import { useSession } from "@/lib/session";

/** Every page in this group needs a session; without one, the user goes to the login page. */
export default function SignedInLayout({ children }: { children: React.ReactNode }) {
  const session = useSession();
  const router = useRouter();
  const pathname = usePathname();
  const signedOut = session.error instanceof ApiError && session.error.status === 401;

  useEffect(() => {
    if (signedOut) router.replace(loginPath(pathname));
  }, [signedOut, pathname, router]);

  if (session.data && !signedOut) return <AppShell session={session.data}>{children}</AppShell>;
  if (session.isError && !signedOut) {
    return (
      <div className="mx-auto w-full max-w-md p-8">
        <ErrorAlert error={session.error} title="Couldn't load your session">
          <Button variant="outline" size="sm" className="mt-3" onClick={() => session.refetch()}>
            Try again
          </Button>
        </ErrorAlert>
      </div>
    );
  }
  return <PageLoading />;
}
