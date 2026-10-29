import { defineConfig, devices } from "@playwright/test";

const CI = Boolean(process.env.CI);

/**
 * Port for the production server under test. Not 3000 (`bun run dev`) so a
 * running dev server never collides with, or is mistaken for, the e2e build.
 */
const PORT = Number(process.env.E2E_PORT) || 3200;

/**
 * Separate build output: the e2e build must never overwrite the `.next`
 * directory a running `next dev` is serving from (see next.config.ts).
 */
const DIST_DIR = ".next-e2e";

export default defineConfig({
  testDir: "./e2e",
  // Real-backend run: its own config (playwright.live.config.ts).
  testIgnore: "live-backend.spec.ts",
  outputDir: "./test-results",
  fullyParallel: true,
  forbidOnly: CI,
  retries: CI ? 2 : 0,
  reporter: CI ? [["github"], ["html", { open: "never" }]] : "list",
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // Locally use the installed Google Chrome (no browser download); CI
        // installs Playwright's bundled Chromium in the workflow.
        ...(CI ? {} : { channel: "chrome" }),
      },
    },
  ],
  webServer: {
    // A production build: the e2e suite checks what ships, not dev mode.
    command: `bun run build && bunx next start --port ${PORT}`,
    url: `http://localhost:${PORT}/runs`,
    reuseExistingServer: !CI,
    timeout: 240_000,
    env: {
      NEXT_DIST_DIR: DIST_DIR,
      NEXT_TELEMETRY_DISABLED: "1",
    },
    // Surface build/start output so CI failures are diagnosable.
    stdout: "pipe",
    stderr: "pipe",
  },
});
