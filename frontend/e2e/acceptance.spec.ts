// M8 acceptance (SPEC.md §17.13, ADR 0013): register → upload → index → ask → grounded
// answer → open citation → follow-up → abstention, through the browser with live Gemini.
// It creates a fresh account each run; remove it afterwards (the app has no account
// deletion; see PROJECT_STATUS.md for the cleanup used).

import { readdirSync } from "node:fs";
import { join } from "node:path";

import { expect, test, type Page } from "@playwright/test";

const DEMO_DIR = join(__dirname, "..", "..", "sample_data", "demo_store");
const KEY_SHAPE = /AIza[0-9A-Za-z_-]{35}/;
const gemini = process.env.GEMINI_API_KEY?.trim();

async function noHorizontalScroll(page: Page) {
  return page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
}

test("a new user can index documents and get cited answers and an abstention", async ({ page }) => {
  // Every API response is checked for a key-shaped string or the configured key. (The
  // variable's name is allowed: /ai/status names it when no key is set.)
  const leaks: string[] = [];
  page.on("response", async (response) => {
    if (!response.url().includes("/api/v1/")) return;
    const body = await response.text().catch(() => "");
    if (KEY_SHAPE.test(body) || (gemini && body.includes(gemini))) {
      leaks.push(response.url());
    }
  });

  const email = `m8-e2e-${Date.now().toString(36)}@example.com`;
  const question = page.getByLabel("Your question");
  const assistantReplies = page.getByTestId("assistant-message");
  const thinking = page.getByText("Searching your documents and writing an answer…");

  await test.step("register", async () => {
    await page.goto("/register");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("m8-e2e-password-0123");
    await page.getByLabel("Workspace name").fill("Northstar Demo");
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard$/);
    await expect(page.getByRole("heading", { level: 1, name: "Northstar Demo" })).toBeVisible();
  });

  const files = readdirSync(DEMO_DIR).filter((name) => name.endsWith(".md")).sort();
  expect(files).toHaveLength(5);

  await test.step("upload the Northstar documents and wait for indexing", async () => {
    await page.getByRole("link", { name: "Documents" }).first().click();
    await expect(page.getByText("No documents yet")).toBeVisible();
    for (const file of files) {
      await page.locator('input[type="file"]').setInputFiles(join(DEMO_DIR, file));
      await expect(page.getByText(`${file} is queued`)).toBeVisible();
    }
    for (const file of files) {
      const row = page.getByRole("row").filter({ hasText: file });
      // Ready appears only when the worker marks the source ready (polled every 3 s).
      await expect(row.getByText("Ready")).toBeVisible({ timeout: 180_000 });
    }
    await page.getByRole("link", { name: "Dashboard" }).click();
    await expect(page.getByText("Indexed chunks")).toBeVisible();
  });

  await test.step("ask a question and get a grounded answer", async () => {
    await page.getByRole("link", { name: "Chat" }).click();
    await question.fill("I bought a laptop 20 days ago. Can I return it?");
    await question.press("Enter");
    await expect(thinking).toBeVisible();
    await expect(assistantReplies).toHaveCount(1, { timeout: 150_000 });
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);
    const answer = assistantReplies.first();
    await expect(answer).toHaveAttribute("data-outcome", "answered");
    await expect(answer.getByRole("button", { name: /^Source S\d+/ }).first()).toBeVisible();
    await expect(answer).toContainText("returns-policy.md");
  });

  await test.step("open the citation and see the supporting excerpt", async () => {
    await assistantReplies.first().getByRole("button", { name: /^Source S\d+/ }).first().click();
    const panel = page.getByRole("dialog");
    await expect(panel).toContainText("Northstar Outfitters Returns Policy");
    await expect(panel).toContainText("returns-policy.md");
    await expect(panel.locator("blockquote")).toContainText("14 days");
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();
  });

  await test.step("ask a follow-up in the same conversation", async () => {
    await question.fill("What if the packaging is damaged?");
    await question.press("Enter");
    await expect(assistantReplies).toHaveCount(2, { timeout: 150_000 });
    const followUp = assistantReplies.nth(1);
    await expect(followUp).toHaveAttribute("data-outcome", "answered");
    await expect(followUp).toContainText("follow-up: searched with your previous question");
  });

  await test.step("ask an unsupported question and see the abstention", async () => {
    await question.fill("Who won the 1998 football World Cup?");
    await question.press("Enter");
    await expect(assistantReplies).toHaveCount(3, { timeout: 150_000 });
    const abstention = assistantReplies.nth(2);
    await expect(abstention).toHaveAttribute("data-outcome", "abstained");
    await expect(abstention).toContainText("Not found in your documents");
    await expect(thinking).toBeHidden();
  });

  await test.step("the conversation is saved", async () => {
    await page.reload();
    await expect(assistantReplies).toHaveCount(3);
  });

  await test.step("narrow screens: no horizontal scroll", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await noHorizontalScroll(page)).toBe(true);
    for (const path of ["/dashboard", "/sources", "/chat"]) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      expect(await noHorizontalScroll(page), path).toBe(true);
    }
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  await test.step("log out", async () => {
    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/login$/);
    await page.goto("/chat");
    await expect(page).toHaveURL(/\/login/);
  });

  expect(leaks, "API responses containing Gemini key material").toEqual([]);
});
