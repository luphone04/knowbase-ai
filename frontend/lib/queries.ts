"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import { sourceDetailPollInterval, sourcesPollInterval } from "./status";

// ["sources"] is a prefix of every source query, so invalidating it refreshes the
// list and any open detail page.
export const sourcesKey = ["sources"] as const;
export const sourceKey = (id: string) => ["sources", id] as const;
export const conversationsKey = ["conversations"] as const;
export const conversationKey = (id: string) => ["conversations", id] as const;

/** Every source in the workspace, polled while the worker still has work for any of them. */
export function useSources() {
  return useQuery({
    queryKey: sourcesKey,
    queryFn: api.sources.list,
    refetchInterval: (query) => sourcesPollInterval(query.state.data),
  });
}

export function useSource(id: string) {
  return useQuery({
    queryKey: sourceKey(id),
    queryFn: () => api.sources.get(id),
    refetchInterval: (query) => sourceDetailPollInterval(query.state.data),
  });
}
