"use client";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "@/lib/api";
import { SESSION_KEY } from "@/lib/session";

function makeQueryClient() {
  // A 401 from any request means the session ended (expired or logged out elsewhere):
  // re-check it, and the app layout sends the user to the login page.
  const onError = (error: Error) => {
    if (error instanceof ApiError && error.status === 401) {
      void client.invalidateQueries({ queryKey: SESSION_KEY });
    }
  };
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({
      onError: (error, query) => {
        if (query.queryKey[0] !== SESSION_KEY[0]) onError(error);
      },
    }),
    mutationCache: new MutationCache({ onError }),
    defaultOptions: {
      queries: {
        // A 4xx won't change on retry (401, 404, ...); one retry covers a brief network blip.
        retry: (failureCount, error) =>
          !(error instanceof ApiError && error.status >= 400 && error.status < 500) &&
          failureCount < 1,
        refetchOnWindowFocus: false,
      },
      // Mutations (uploads, questions) are never retried: a retry could spend Gemini quota twice.
      mutations: { retry: false },
    },
  });
  return client;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(makeQueryClient);
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
