import { defineConfig, devices } from "@playwright/test";

// The acceptance test drives the real stack: the frontend (Compose or `npm run dev`),
// the API, the worker, and live Gemini. It is run by hand, not in CI (no Gemini key there).
export default defineConfig({
  testDir: "e2e",
  timeout: 10 * 60 * 1000,
  expect: { timeout: 15_000 },
  retries: 0, // a retry would spend Gemini quota again
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
