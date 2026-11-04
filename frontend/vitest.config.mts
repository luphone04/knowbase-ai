import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

// Unit tests for the pure logic in lib/, and component tests (jsdom, chosen per file with
// `// @vitest-environment jsdom`). The browser flow is covered by Playwright (e2e/).
export default defineConfig({
  resolve: { alias: { "@": fileURLToPath(new URL(".", import.meta.url)) } },
  test: {
    include: ["lib/**/*.test.ts", "components/**/*.test.tsx"],
    environment: "node",
    env: { NEXT_PUBLIC_API_BASE_URL: "http://api.test/api/v1" },
  },
});
