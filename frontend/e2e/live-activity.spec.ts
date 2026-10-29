import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, type Page, test } from "@playwright/test";
import type { RunEvent, RunEventOf } from "../src/lib/api/types";

/*
 * Round 2 on the real backend: one real upload with the table left on Auto.
 *
 * Opt-in like live-backend.spec.ts (SLUICE_E2E_API_URL, run through
 * playwright.live.config.ts). The file is read as is, never edited:
 * SLUICE_E2E_UPLOAD names it (default: the demo's orders-15k-hard.json).
 * A file already loaded once is skipped by the backend ("already loaded"),
 * so the spec accepts that path and asserts what it can there.
 *
 * Fresh path: the live activity line appears on a node phase and its clock
 * ticks, the raw "Uploaded file (not loaded)" view shows before `loaded`,
 * the run finishes, and a retried attempt on the strong tier shows a
 * "strong" chip while it runs.
 */

const API = process.env.SLUICE_E2E_API_URL?.trim().replace(/\/+$/, "") ?? "";
const UPLOAD =
  process.env.SLUICE_E2E_UPLOAD?.trim() ||
  path.join(
    process.env.HOME ?? "",
    "Developer/sluice/backend/cases/demo/upload-me/orders-15k-hard.json",
  );
const SHOTS = process.env.SLUICE_E2E_SHOTS?.trim() || "test-results/live";
const NODE_PHASES = new Set(["writing", "sandbox", "checking", "verifying"]);

const MIME: Record<string, string> = {
  ".csv": "text/csv",
  ".json": "application/json",
  ".parquet": "application/vnd.apache.parquet",
};

async function replayEvents(runId: string): Promise<RunEvent[]> {
  const res = await fetch(`${API}/runs/${encodeURIComponent(runId)}/events`, {
    headers: { Accept: "text/event-stream" },
  });
  expect(res.status).toBe(200);
  const events: RunEvent[] = [];
  for (const block of (await res.text()).split(/\r\n\r\n|\n\n|\r\r/)) {
    const data = block
      .split(/\r\n|\n|\r/)
      .filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).replace(/^ /, ""));
    if (data.length > 0) events.push(JSON.parse(data.join("\n")) as RunEvent);
  }
  return events;
}

const eventsOf = <T extends RunEvent["type"]>(events: RunEvent[], type: T) =>
  events.filter((e): e is RunEventOf<T> => e.type === type);

interface Watch {
  /** `performance.now()` when the raw upload view first showed. */
  rawAt: number | null;
  /** When a "Loaded … rows" step first showed. */
  loadedAt: number | null;
  /** Chips seen on step rows that were on attempt 2 or later. */
  retryChips: string[];
  /** Phases the activity line showed, in order of first sight. */
  phases: string[];
}

/** Records milestones from page load with a MutationObserver. */
async function watch(page: Page) {
  await page.addInitScript(() => {
    const w: Watch = {
      rawAt: null,
      loadedAt: null,
      retryChips: [],
      phases: [],
    };
    Object.assign(window, { sluiceWatch: w });
    const check = () => {
      const now = performance.now();
      if (
        w.rawAt === null &&
        [...document.querySelectorAll("h2")].some(
          (h) => h.textContent === "Uploaded file (not loaded)",
        )
      ) {
        w.rawAt = now;
      }
      const rows = [
        ...document.querySelectorAll('ol[aria-label="Pipeline steps"] > li'),
      ];
      for (const li of rows) {
        const text = li.textContent ?? "";
        if (w.loadedAt === null && /Loaded [\d,]+ rows/.test(text)) {
          w.loadedAt = now;
        }
        if (/attempt [2-9]\d*\//.test(text)) {
          const chip = li.querySelector(":scope > span.rounded-full");
          const tier = chip?.textContent ?? "";
          if (tier && !w.retryChips.includes(tier)) w.retryChips.push(tier);
        }
      }
      for (const line of document.querySelectorAll(
        '[data-testid="activity-line"]',
      )) {
        const phase = line.getAttribute("data-phase") ?? "";
        if (phase && w.phases.at(-1) !== phase) w.phases.push(phase);
      }
    };
    new MutationObserver(check).observe(document, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
    });
  });
}

/** The clock's number of seconds ("12 s", "1 m 05 s"), or null. */
function seconds(text: string): number | null {
  const m = / · (?:(\d+) m )?(\d+) s$/.exec(text);
  if (!m) return null;
  return Number(m[1] ?? 0) * 60 + Number(m[2]);
}

test.describe("live backend: activity, auto table, raw upload", () => {
  test.skip(!API, "set SLUICE_E2E_API_URL to run against a real backend");

  test("an Auto upload shows live activity and the raw file, then finishes", async ({
    page,
  }) => {
    const ext = path.extname(UPLOAD);
    const file = {
      name: path.basename(UPLOAD),
      mimeType: MIME[ext] ?? "application/octet-stream",
      buffer: readFileSync(UPLOAD),
    };
    const problems: string[] = [];
    page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
    await watch(page);

    await page.goto("/runs");
    const composer = page.getByRole("form", { name: "New pipeline run" });
    const picker = composer.getByLabel("Target table");
    await expect(picker).toHaveValue("auto");
    await composer.getByLabel("Attach a file").setInputFiles(file);
    await composer.getByRole("button", { name: "Start run" }).click();

    await expect(page).toHaveURL(/\/runs\/[A-Za-z0-9_-]{1,64}$/, {
      timeout: 60_000,
    });
    const runId = decodeURIComponent(
      new URL(page.url()).pathname.split("/").at(-1) ?? "",
    );
    test.info().annotations.push({ type: "run", description: runId });

    // Auto: the thread names the table the server matched.
    await expect(
      page.getByText(/^Matched the (orders|lineitem) table$/),
    ).toBeVisible({
      timeout: 30_000,
    });

    // The raw upload shows from creation, labelled as not loaded.
    const details = page.getByRole("complementary", { name: "Details" });
    await expect(
      details.getByRole("heading", { name: "Uploaded file (not loaded)" }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      details
        .getByRole("table", { name: "Uploaded file" })
        .locator("tbody tr")
        .first(),
    ).toBeVisible();

    // Fresh run or "already loaded" skip: whichever shows first.
    const summary = page.getByRole("region", { name: "Run summary" });
    const activity = page.getByTestId("activity-line");
    await expect(activity.or(summary).first()).toBeVisible({
      timeout: 120_000,
    });

    let ticked: { phase: string; from: number; to: number } | null = null;
    let midShot = false;
    // Watch for a node phase whose clock moves on while it stays current.
    while (!ticked && !(await summary.isVisible())) {
      const lines = await activity.all();
      for (const line of lines) {
        const phase =
          (await line.getAttribute("data-phase").catch(() => null)) ?? "";
        const before = await line.textContent().catch(() => null);
        if (!NODE_PHASES.has(phase) || before === null) continue;
        const from = seconds(before);
        await page.waitForTimeout(2_200);
        const after = await line.textContent().catch(() => null);
        const to = after === null ? null : seconds(after);
        const samePhase =
          (await line.getAttribute("data-phase").catch(() => null)) === phase;
        if (from !== null && to !== null && samePhase && to > from) {
          ticked = { phase, from, to };
          await page.screenshot({ path: `${SHOTS}/mid-run.png` });
          midShot = true;
        }
        break;
      }
      if (!ticked) await page.waitForTimeout(500);
    }
    if (!midShot && (await activity.count()) > 0) {
      await page.screenshot({ path: `${SHOTS}/mid-run.png` });
    }

    await expect(summary).toBeVisible({ timeout: 9 * 60_000 });
    await expect(activity).toHaveCount(0);

    const events = await replayEvents(runId);
    expect(events.map((e) => e.seq)).toEqual(events.map((_, i) => i + 1));
    const started = eventsOf(events, "run_started")[0];
    expect(started?.table === "orders" || started?.table === "lineitem").toBe(
      true,
    );
    const final = eventsOf(events, "run_finished")[0];
    expect(events.at(-1)?.type).toBe("run_finished");
    const plan = eventsOf(events, "plan_created")[0];
    const w = await page.evaluate(
      () => (window as unknown as { sluiceWatch: Watch }).sluiceWatch,
    );
    const shown = eventsOf(events, "activity");

    if (!plan) {
      test.info().annotations.push({
        type: "outcome",
        description: `no plan (${final?.status}): ${final?.summary}`,
      });
      // A refused or skipped file still shows what was uploaded.
      await expect(
        details.getByRole("heading", { name: "Uploaded file (not loaded)" }),
      ).toBeVisible();
    } else {
      test.info().annotations.push({
        type: "outcome",
        description: `fresh run, ${final?.status}: ${final?.summary}`,
      });
      expect(["succeeded", "quarantined"]).toContain(final?.status);
      expect(shown.length, "activity events streamed").toBeGreaterThan(0);
      expect(ticked, "a node phase's clock ticked on screen").not.toBeNull();
      test.info().annotations.push({
        type: "ticked",
        description: JSON.stringify(ticked),
      });
      // The raw view came before the load.
      expect(w.rawAt, "raw upload shown").not.toBeNull();
      if (eventsOf(events, "loaded").length > 0) {
        expect(w.rawAt ?? Number.POSITIVE_INFINITY).toBeLessThan(
          w.loadedAt ?? Number.POSITIVE_INFINITY,
        );
        await expect(
          details.getByRole("table", { name: "Before and after values" }),
        ).toBeVisible({ timeout: 30_000 });
      }
      // A retry on the strong tier said so while it ran.
      const strongRetry = eventsOf(events, "node_started").some(
        (e) => e.attempt > 1 && e.tier === "strong",
      );
      test.info().annotations.push({
        type: "retries",
        description: `strong retry: ${strongRetry}; chips seen on retries: ${w.retryChips.join(", ") || "none"}`,
      });
      if (strongRetry) expect(w.retryChips).toContain("strong");
    }
    test.info().annotations.push({
      type: "phases",
      description: `on screen: ${w.phases.join(" → ") || "none"}; streamed: ${shown.map((e) => e.phase).join(" → ") || "none"}`,
    });

    await page.screenshot({ path: `${SHOTS}/end.png`, fullPage: true });
    expect(problems).toEqual([]);
  });
});
