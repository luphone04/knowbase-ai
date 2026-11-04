// Every request to the FastAPI backend goes through here (SPEC.md §12.2).
// The session is an HttpOnly cookie set by the API: it is sent with
// `credentials: "include"` and never read or stored by this code.

import { API_BASE_URL } from "./config";
import type {
  AIStatus,
  AnswerMode,
  ApiErrorBody,
  ChatTurn,
  Conversation,
  ConversationDetail,
  EvaluationOverview,
  EvaluationRun,
  EvaluationRunDetail,
  Job,
  LoginInput,
  RegisterInput,
  Session,
  Source,
  SourceDetail,
  UploadResult,
} from "./types";

export type FieldError = { loc: (string | number)[]; msg: string; type: string };

/** A failed request: the API's {"error": {code, message, details}}, or a network failure (status 0). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: FieldError[] = [],
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function isErrorBody(body: unknown): body is ApiErrorBody {
  if (typeof body !== "object" || body === null || !("error" in body)) return false;
  const error = (body as { error: unknown }).error;
  return typeof error === "object" && error !== null && "code" in error && "message" in error;
}

export async function toApiError(response: Response): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  const retryAfter = Number(response.headers.get("Retry-After"));
  const retryAfterSeconds = Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null;
  if (isErrorBody(body)) {
    const { code, message, details } = body.error;
    return new ApiError(response.status, code, message, details ?? [], retryAfterSeconds);
  }
  return new ApiError(response.status, "http_error", `Request failed (${response.status}).`);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      credentials: "include",
      cache: "no-store",
    });
  } catch {
    // Unreachable API, or a response without CORS headers (which the browser hides).
    throw new ApiError(0, "network_error", "Could not reach the API.");
  }
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function json(method: "POST" | "PATCH", body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export const api = {
  auth: {
    me: () => request<Session>("/auth/me"),
    login: (input: LoginInput) => request<Session>("/auth/login", json("POST", input)),
    register: (input: RegisterInput) => request<Session>("/auth/register", json("POST", input)),
    logout: () => request<void>("/auth/logout", { method: "POST" }),
  },
  sources: {
    /** Every source in the workspace (the endpoint has no limit or paging). */
    list: () => request<Source[]>("/sources"),
    get: (id: string) => request<SourceDetail>(`/sources/${encodeURIComponent(id)}`),
    upload: (file: File) => {
      const form = new FormData();
      form.append("file", file); // the browser sets the multipart boundary
      return request<UploadResult>("/sources/files", { method: "POST", body: form });
    },
    reindex: (id: string) =>
      request<Job>(`/sources/${encodeURIComponent(id)}/reindex`, { method: "POST" }),
    remove: (id: string) =>
      request<Job>(`/sources/${encodeURIComponent(id)}`, { method: "DELETE" }),
  },
  conversations: {
    /** The newest 100 of the caller's conversations (the API's limit). */
    list: () => request<Conversation[]>("/conversations"),
    create: () => request<Conversation>("/conversations", json("POST", {})),
    get: (id: string) => request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`),
    ask: (id: string, content: string) =>
      request<ChatTurn>(
        `/conversations/${encodeURIComponent(id)}/messages`,
        json("POST", { content, mode: "hybrid" }),
      ),
  },
  ai: {
    status: () => request<AIStatus>("/ai/status"),
  },
  evaluations: {
    /** The dataset, whether this workspace holds its documents, and today's budget. */
    overview: () => request<EvaluationOverview>("/evaluations/questions"),
    /** Queue a run (202); the worker evaluates it one question at a time. */
    start: (answerModes: AnswerMode[]) =>
      request<EvaluationRun>("/evaluations/run", json("POST", { answer_modes: answerModes })),
    /** The newest 50 runs in this workspace. */
    runs: () => request<EvaluationRun[]>("/evaluations/runs"),
    run: (id: string) => request<EvaluationRunDetail>(`/evaluations/runs/${encodeURIComponent(id)}`),
  },
};
