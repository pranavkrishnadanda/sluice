/*
 * Records the README demo: one real file through the live UI, end to end.
 *
 *   make demo-gif            (repo root: records, then converts to a GIF)
 *   bun scripts/record-demo.ts   (here: records the video only)
 *
 * Needs the services and the API (`make up && make api`, the API started
 * with `SLUICE_UI_ORIGINS=http://localhost:3210,http://127.0.0.1:3210`). It
 * builds the UI against that API into `.next-e2e-live/` (the live e2e build)
 * and serves it on 3210, unless a UI is already answering there.
 *
 * The run makes real model calls. A file is loaded at most once, so the
 * case must be one the backend hasn't loaded yet: a repeat is skipped as
 * "already loaded" and shows no plan, and the script stops with an error.
 *
 * Output: `test-results/demo/sluice-demo.webm`, 1440×900. The stretch
 * between the plan appearing and the run finishing is sped up (to about
 * WAIT_TARGET_S seconds) so the GIF stays short; everything else plays in
 * real time.
 */
import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import {
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, type Page } from "@playwright/test";

const API = (
  process.env.SLUICE_DEMO_API_URL?.trim() || "http://localhost:8080"
).replace(/\/+$/, "");
const PORT = Number(process.env.SLUICE_DEMO_PORT) || 3210;
const UI = `http://localhost:${PORT}`;
const CASE_ID = process.env.SLUICE_DEMO_CASE?.trim() || "dev-0039";
const CASES_DIR =
  process.env.SLUICE_DEMO_CASES_DIR?.trim() ||
  fileURLToPath(new URL("../../backend/cases/dev/", import.meta.url));
const FRONTEND = fileURLToPath(new URL("../", import.meta.url));
const OUT_DIR = path.join(FRONTEND, "test-results", "demo");
const OUT = path.join(OUT_DIR, "sluice-demo.webm");
const FFMPEG = process.env.FFMPEG?.trim() || "ffmpeg";
const SIZE = { width: 1440, height: 900 };
/** The sped-up wait lasts about this long in the output. */
const WAIT_TARGET_S = 5;
const RUN_TIMEOUT_MS = 9 * 60_000;

const MIME: Record<string, string> = {
  ".csv": "text/csv",
  ".json": "application/json",
  ".parquet": "application/vnd.apache.parquet",
};

class DemoError extends Error {}

function fail(message: string): never {
  throw new DemoError(message);
}

async function reachable(url: string): Promise<boolean> {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(3_000) });
    return res.ok;
  } catch {
    return false;
  }
}

function loadCase(id: string) {
  const dir = path.join(CASES_DIR, id);
  let manifest: { table: "orders" | "lineitem" };
  try {
    manifest = JSON.parse(
      readFileSync(path.join(dir, "manifest.json"), "utf8"),
    );
  } catch {
    fail(
      `case ${id} not found in ${CASES_DIR} (run \`make cases\`, or set SLUICE_DEMO_CASES_DIR)`,
    );
  }
  const input = readdirSync(dir).find((f) => f.startsWith("input."));
  if (!input) fail(`no input.* in case ${id}`);
  const ext = path.extname(input);
  return {
    table: manifest.table,
    file: {
      name: `${id}${ext}`,
      mimeType: MIME[ext] ?? "application/octet-stream",
      buffer: readFileSync(path.join(dir, input)),
    },
  };
}

/** Builds the UI against the API and serves it, unless one is already up. */
async function startUi(): Promise<ChildProcess | null> {
  if (await reachable(`${UI}/runs`)) return null;
  console.log(
    `record-demo: building the UI against ${API} and serving it on ${PORT}`,
  );
  const server = spawn(
    "sh",
    ["-c", `bun run build && bunx next start --port ${PORT}`],
    {
      cwd: FRONTEND,
      env: {
        ...process.env,
        NEXT_DIST_DIR: ".next-e2e-live",
        NEXT_PUBLIC_SLUICE_API_URL: API,
        NEXT_TELEMETRY_DISABLED: "1",
      },
      stdio: ["ignore", "ignore", "inherit"],
      detached: true,
    },
  );
  const deadline = Date.now() + 240_000;
  while (Date.now() < deadline) {
    if (server.exitCode !== null) fail("the UI build or server exited early");
    if (await reachable(`${UI}/runs`)) return server;
    await new Promise((r) => setTimeout(r, 1_000));
  }
  stopUi(server);
  fail(`the UI did not answer on ${UI} within 4 minutes`);
}

function stopUi(server: ChildProcess | null): void {
  if (server?.pid) {
    try {
      process.kill(-server.pid, "SIGTERM");
    } catch {
      // already gone
    }
  }
}

/** A readable pause: the viewer gets time to take in each screen. */
const hold = (page: Page, ms: number) => page.waitForTimeout(ms);

interface Marks {
  /** Seconds from the start of the video. */
  start: number;
  waitFrom: number;
  waitTo: number;
}

async function record(page: Page, t0: number): Promise<Marks> {
  const at = () => (Date.now() - t0) / 1_000;
  const { table, file } = loadCase(CASE_ID);

  await page.goto(`${UI}/runs`);
  await page
    .getByRole("heading", { level: 1, name: "New pipeline run" })
    .waitFor();
  if ((await page.getByText("Demo data").count()) > 0) {
    fail("the UI shows the demo badge: it was not built against the API");
  }
  const start = Math.max(0, at() - 0.2);
  await hold(page, 1_500);

  const composer = page.getByRole("form", { name: "New pipeline run" });
  await composer.getByLabel("Attach a file").setInputFiles(file);
  await hold(page, 700);
  await composer.getByLabel("Target table").selectOption(table);
  await hold(page, 900);
  await composer.getByRole("button", { name: "Start run" }).click();
  await page.waitForURL(/\/runs\/[A-Za-z0-9_-]{1,64}$/, { timeout: 30_000 });

  // Real time while the profile, rule fixes and plan appear; the long
  // worker / sandbox / verify stretch after that is sped up.
  const steps = page.locator('ol[aria-label="Pipeline steps"] > li');
  await page
    .waitForFunction(
      () => {
        const rows = [
          ...document.querySelectorAll('ol[aria-label="Pipeline steps"] > li'),
        ];
        return (
          rows.length >= 3 &&
          !rows.some((li) =>
            (li.textContent ?? "").includes("Planning repairs"),
          )
        );
      },
      undefined,
      { timeout: RUN_TIMEOUT_MS },
    )
    .catch(() => undefined);
  await hold(page, 1_500);
  const waitFrom = at();

  const summary = page.getByRole("region", { name: "Run summary" });
  await summary.waitFor({ timeout: RUN_TIMEOUT_MS });
  const waitTo = Math.max(waitFrom, at() - 0.5);
  if ((await summary.textContent())?.includes("already loaded")) {
    fail(
      `${CASE_ID} was already loaded, so nothing ran: pick a fresh case with SLUICE_DEMO_CASE`,
    );
  }
  if ((await steps.count()) < 3) fail("the run shows no plan");
  await summary.scrollIntoViewIfNeeded();
  await hold(page, 2_000);

  const details = page.getByRole("complementary", { name: "Details" });
  await details.getByRole("tab", { name: "Plan" }).click();
  await hold(page, 2_500);
  await details.getByRole("tab", { name: "Code" }).click();
  await hold(page, 3_000);
  await details.getByRole("tab", { name: "Data" }).click();
  await details
    .getByRole("table", { name: "Before and after values" })
    .waitFor({ timeout: 30_000 })
    .catch(() => undefined);
  await hold(page, 3_000);

  // Dark (the system scheme) to light: one press of the theme toggle.
  await page
    .getByRole("button", { name: /^Theme: / })
    .first()
    .click();
  await hold(page, 2_500);
  return { start, waitFrom, waitTo };
}

/** Trims the blank lead-in and speeds up the wait for the run to finish. */
function compress(raw: string, marks: Marks): void {
  const wait = marks.waitTo - marks.waitFrom;
  const factor = Math.max(1, wait / WAIT_TARGET_S);
  const f = (n: number) => n.toFixed(3);
  const filter = [
    `[0:v]trim=start=${f(marks.start)}:end=${f(marks.waitFrom)},setpts=PTS-STARTPTS[a]`,
    `[0:v]trim=start=${f(marks.waitFrom)}:end=${f(marks.waitTo)},setpts=(PTS-STARTPTS)/${f(factor)}[b]`,
    `[0:v]trim=start=${f(marks.waitTo)},setpts=PTS-STARTPTS[c]`,
    "[a][b][c]concat=n=3:v=1:a=0[out]",
  ].join(";");
  const result = spawnSync(
    FFMPEG,
    [
      "-y",
      "-loglevel",
      "error",
      "-i",
      raw,
      "-filter_complex",
      filter,
      "-map",
      "[out]",
      "-c:v",
      "libvpx-vp9",
      "-deadline",
      "realtime",
      "-b:v",
      "0",
      "-crf",
      "18",
      OUT,
    ],
    { stdio: "inherit" },
  );
  if (result.error || result.status !== 0)
    fail(`ffmpeg failed (is it installed? set FFMPEG)`);
  console.log(
    `record-demo: wait ${wait.toFixed(1)} s sped up ${factor.toFixed(1)}x; video at ${path.relative(FRONTEND, OUT)}`,
  );
}

async function main(): Promise<void> {
  if (!(await reachable(`${API}/runs`))) {
    fail(
      `the API is not reachable at ${API}: start it with \`make up && make api\` (set SLUICE_DEMO_API_URL to use another URL)`,
    );
  }
  loadCase(CASE_ID);
  rmSync(OUT_DIR, { recursive: true, force: true });
  mkdirSync(OUT_DIR, { recursive: true });

  const server = await startUi();
  const browser = await chromium.launch({
    channel: process.env.SLUICE_DEMO_CHANNEL ?? "chrome",
  });
  try {
    const context = await browser.newContext({
      viewport: SIZE,
      colorScheme: "dark",
      reducedMotion: "no-preference",
      recordVideo: { dir: OUT_DIR, size: SIZE },
    });
    const page = await context.newPage();
    const t0 = Date.now();
    const marks = await record(page, t0);
    const video = page.video();
    await context.close();
    if (!video) fail("no video was recorded");
    const raw = path.join(OUT_DIR, "raw.webm");
    renameSync(await video.path(), raw);
    compress(raw, marks);
  } finally {
    await browser.close();
    stopUi(server);
  }
}

try {
  await main();
} catch (err) {
  console.error(
    `record-demo: ${err instanceof DemoError ? err.message : String(err)}`,
  );
  process.exit(1);
}
