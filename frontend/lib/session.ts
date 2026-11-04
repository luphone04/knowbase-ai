"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "./api";

export const SESSION_KEY = ["session"] as const;

/** The signed-in user and workspace, from GET /auth/me (the cookie itself is unreadable here). */
export function useSession() {
  return useQuery({ queryKey: SESSION_KEY, queryFn: api.auth.me, staleTime: Infinity, retry: false });
}
