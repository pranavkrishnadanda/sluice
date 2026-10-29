import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright against the real backend: `e2e/live-backend.spec.ts` only.
 *
 * Opt-in and never part of `bun run e2e` or CI. It needs the services and
 * the API running (`make up && make api`) and makes real model calls:
 *
 *   SLUICE_E2E_API_URL=http://localhost:8080 bun run e2e:live
 *
 * The UI is a production build with `NEXT_PUBLIC_SLUICE_API_URL` set to
 * `SLUICE_E2E_API_URL`, in its own build directory and on its own port, so
 * it never collides with the mock-backed e2e build (`.next-e2e`, port 3200)
 * or a running `next dev`. The API must allow this origin
 * (`SLUICE_UI_ORIGINS=http://localhost:3210,...` when starting it).
 */
const API_URL = process.env.SLUICE_E2E_API_URL?.trim() ?? "";
const PORT = Number(process.env.E2E_LIVE_PORT) || 3210;
const DIST_DIR = ".next-e2e-live";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "live-backend.spec.ts",
  outputDir: "./test-results/live",
  // One real run at a time: it shares the backend's worker and model quota.
  workers: 1,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: "list",
  // A real run calls models (free-tier rate limits included).
  timeout: 10 * 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "retain-on-failure",
    // Fail a stuck click fast instead of waiting out the 10-minute budget.
    actionTimeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], channel: "chrome" },
    },
  ],
  // Without an API URL the spec skips itself; don't build a UI for nothing.
  webServer: API_URL
    ? {
        command: `bun run build && bunx next start --port ${PORT}`,
        url: `http://localhost:${PORT}/runs`,
        reuseExistingServer: false,
        timeout: 240_000,
        env: {
          NEXT_DIST_DIR: DIST_DIR,
          NEXT_PUBLIC_SLUICE_API_URL: API_URL,
          NEXT_TELEMETRY_DISABLED: "1",
        },
        stdout: "pipe",
        stderr: "pipe",
      }
    : undefined,
});
