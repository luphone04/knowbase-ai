"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import { runPollInterval, runsPollInterval } from "./evaluation";
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

// ["evaluations"] prefixes every evaluation query, so starting a run refreshes them all.
export const evaluationsKey = ["evaluations"] as const;
export const evaluationOverviewKey = ["evaluations", "overview"] as const;
export const evaluationRunsKey = ["evaluations", "runs"] as const;
export const evaluationRunKey = (id: string) => ["evaluations", "runs", id] as const;

export function useEvaluationOverview() {
  return useQuery({ queryKey: evaluationOverviewKey, queryFn: api.evaluations.overview });
}

/** This workspace's runs, polled while one of them is queued or running. */
export function useEvaluationRuns() {
  return useQuery({
    queryKey: evaluationRunsKey,
    queryFn: api.evaluations.runs,
    refetchInterval: (query) => runsPollInterval(query.state.data),
  });
}

/** One run with its per-question results, polled until it finishes. */
export function useEvaluationRun(id: string) {
  return useQuery({
    queryKey: evaluationRunKey(id),
    queryFn: () => api.evaluations.run(id),
    refetchInterval: (query) => runPollInterval(query.state.data),
  });
}
