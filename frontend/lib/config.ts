// Build-time settings. Next.js inlines NEXT_PUBLIC_* values into the browser
// bundle, so nothing here may ever be a secret (the Gemini key stays in the backend).

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL;
if (!apiBaseUrl) {
  throw new Error(
    "NEXT_PUBLIC_API_BASE_URL is not set (for example http://localhost:8001/api/v1).",
  );
}

/** The FastAPI base URL, including /api/v1, without a trailing slash. */
export const API_BASE_URL = apiBaseUrl.replace(/\/+$/, "");

/** Mirrors the backend's MAX_UPLOAD_MB. Only a pre-check: the server enforces the limit. */
export const MAX_UPLOAD_MB = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB ?? "10");
