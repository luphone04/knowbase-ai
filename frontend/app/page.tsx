"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { PageLoading } from "@/components/page-loading";
import { ApiError } from "@/lib/api";
import { HOME_PATH } from "@/lib/redirect";
import { useSession } from "@/lib/session";

/** "/" goes to the login page without a session, otherwise to the dashboard. */
export default function Home() {
  const session = useSession();
  const router = useRouter();

  useEffect(() => {
    if (session.error instanceof ApiError && session.error.status === 401) {
      router.replace("/login");
    } else if (session.data || session.error) {
      // Any other failure (API unreachable, ...) is shown by the signed-in layout.
      router.replace(HOME_PATH);
    }
  }, [session.data, session.error, router]);

  return <PageLoading />;
}
