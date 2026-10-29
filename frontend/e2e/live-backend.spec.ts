import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type Page, test } from "@playwright/test";
import type { RunEvent, RunEventOf, Transform } from "../src/lib/api/types";
import { scan } from "./helpers";

/*
 * The UI on the real backend: one real file, end to end.
 *
 * Opt-in: skipped unless SLUICE_E2E_API_URL is set, and run through
 * `playwright.live.config.ts` (`bun run e2e:live`), which builds the UI
 * against that API. It needs `make up && make api` and makes real model
 * calls, so it uploads exactly one small dev case per run.
 *
 * A file is loaded at most once (the idempotency key is the table, the
 * file's SHA-256 and the contract version), so a repeat run of the same
 * case is reported "file already loaded" without running the pipeline. The
 * spec accepts either outcome and asserts what the stream says happened.
 */

const API = process.env.SLUICE_E2E_API_URL?.trim().replace(/\/+$/, "") ?? "";
const CASE_ID = process.env.SLUICE_E2E_CASE?.trim() || "dev-0025";
const CASES_DIR =
  process.env.SLUICE_E2E_CASES_DIR?.trim() ||
  fileURLToPath(new URL("../../backend/cases/dev/", import.meta.url));

const MIME: Record<string, string> = {
  ".csv": "text/csv",
  ".json": "application/json",
  ".parquet": "application/vnd.apache.parquet",
};

interface Case {
  table: "orders" | "lineitem";
  /** Uploaded under a readable name; the bytes are the case's input. */
  name: string;
  mimeType: string;
  buffer: Buffer;
}

function loadCase(id: string): Case {
  const dir = path.join(CASES_DIR, id);
  const manifest = JSON.parse(
    readFileSync(path.join(dir, "manifest.json"), "utf8"),
  ) as { table: Case["table"] };
  const input = readdirSync(dir).find((f) => f.startsWith("input."));
  if (!input) throw new Error(`No input.* in ${dir}`);
  const ext = path.extname(input);
  return {
    table: manifest.table,
    name: `${id}${ext}`,
    mimeType: MIME[ext] ?? "application/octet-stream",
    buffer: readFileSync(path.join(dir, input)),
  };
}

/** The run's full event history: the stream replays it, then closes. */
async function replayEvents(runId: string): Promise<RunEvent[]> {
  const res = await fetch(`${API}/runs/${encodeURIComponent(runId)}/events`, {
    headers: { Accept: "text/event-stream" },
  });
  expect(res.status).toBe(200);
  expect(res.headers.get("content-type")).toMatch(/^text\/event-stream/);
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

async function getTransforms(runId: string): Promise<Transform[]> {
  const res = await fetch(
    `${API}/runs/${encodeURIComponent(runId)}/transforms`,
  );
  expect(res.status).toBe(200);
  return (await res.json()) as Transform[];
}

function eventsOf<T extends RunEvent["type"]>(
  events: RunEvent[],
  type: T,
): RunEventOf<T>[] {
  return events.filter((e): e is RunEventOf<T> => e.type === type);
}

/** When each milestone first appeared, in `performance.now()` ms. */
interface Timeline {
  planAt: number | null;
  summaryAt: number | null;
  codeAt: number | null;
}

/**
 * Records, from page load, when the thread first shows the plan (a row
 * past "Rules" other than "Planning repairs…") and when the run summary
 * (rendered on `run_finished`) appears, so the spec can tell a live stream
 * from a history that arrived all at once. Also records when the details
 * panel first lists an attempt as passed (the Code tab's accepted transform).
 */
async function recordThreadTimeline(page: Page) {
  await page.addInitScript(() => {
    const timeline: Timeline = { planAt: null, summaryAt: null, codeAt: null };
    Object.assign(window, { sluiceE2eTimeline: timeline });
    const check = () => {
      const now = performance.now();
      const rows = [
        ...document.querySelectorAll('ol[aria-label="Pipeline steps"] > li'),
      ].map((li) => li.textContent ?? "");
      if (
        timeline.planAt === null &&
        rows.length >= 3 &&
        !rows.some((t) => t.includes("Planning repairs"))
      ) {
        timeline.planAt = now;
      }
      if (
        timeline.codeAt === null &&
        [
          ...document.querySelectorAll(
            'aside[aria-label="Details"] fieldset label',
          ),
        ].some((l) => (l.textContent ?? "").includes("passed"))
      ) {
        timeline.codeAt = now;
      }
      if (
        timeline.summaryAt === null &&
        document.querySelector('section[aria-label="Run summary"]')
      ) {
        timeline.summaryAt = now;
      }
    };
    new MutationObserver(check).observe(document, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  });
}

const STATUS_LABEL = {
  succeeded: "Succeeded",
  quarantined: "Quarantined",
  failed: "Failed",
} as const;

test.describe("live backend", () => {
  test.skip(!API, "set SLUICE_E2E_API_URL to run against a real backend");

  test("a real file streams through the live backend to a loaded run", async ({
    page,
  }) => {
    const input = loadCase(CASE_ID);
    const problems: string[] = [];
    // Preview answers: a 409 `not_ready` right after `loaded` is a state the
    // contract documents ("retry shortly"), and Chrome logs every 4xx fetch
    // as a console error, so those (and only those) are checked separately.
    const previews: { status: number; code: string | null }[] = [];
    page.on("response", async (res) => {
      if (!new URL(res.url()).pathname.endsWith("/preview")) return;
      const body = res.ok()
        ? null
        : ((await res.json().catch(() => null)) as { code?: string } | null);
      previews.push({ status: res.status(), code: body?.code ?? null });
    });
    page.on("console", (msg) => {
      if (msg.type() !== "error") return;
      const from = msg.location().url;
      const previewConflict =
        /status of 409/.test(msg.text()) &&
        from.startsWith(API) &&
        new URL(from).pathname.endsWith("/preview");
      if (!previewConflict) problems.push(`console: ${msg.text()}`);
    });
    page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
    await recordThreadTimeline(page);

    // New run: the live client, so no demo badge anywhere.
    await page.goto("/runs");
    await expect(
      page.getByRole("heading", { level: 1, name: "New pipeline run" }),
    ).toBeVisible();
    await expect(page.getByText("Demo data")).toHaveCount(0);

    const composer = page.getByRole("form", { name: "New pipeline run" });
    await composer.getByLabel("Attach a file").setInputFiles(input);
    await composer.getByLabel("Target table").selectOption(input.table);
    await composer.getByRole("button", { name: "Start run" }).click();

    await expect(page).toHaveURL(/\/runs\/[A-Za-z0-9_-]{1,64}$/, {
      timeout: 30_000,
    });
    const runId = decodeURIComponent(
      new URL(page.url()).pathname.split("/").at(-1) ?? "",
    );

    // Open the Code tab now, while the run is in flight, and keep it open:
    // it refetches transforms only when an attempt fails or finishes, so an
    // accepted transform it shows later came from the live run, not a
    // refetch after the end.
    const details = page.getByRole("complementary", { name: "Details" });
    await details.getByRole("tab", { name: "Code" }).click();

    // The new run is listed (and current) in the sidebar.
    // (By its link: a repeat upload of the case lists the same file name.)
    const nav = page.getByRole("navigation", { name: "Primary" });
    const runLink = nav.locator(`a[href="/runs/${encodeURIComponent(runId)}"]`);
    await expect(runLink).toHaveAttribute("aria-current", "page");
    await expect(runLink).toHaveAccessibleName(
      new RegExp(`^${escapeRegExp(input.name)}\\s*, `),
    );

    // The thread names the file and table straight from `run_started`.
    await expect(
      page.getByText(`Load this into the ${input.table} table.`),
    ).toBeVisible({ timeout: 30_000 });

    const summary = page.getByRole("region", { name: "Run summary" });
    await expect(summary).toBeVisible({ timeout: 9 * 60_000 });
    await expect(page.getByText("Demo data")).toHaveCount(0);

    // What the backend says happened, independently of the UI.
    const events = await replayEvents(runId);
    expect(events.map((e) => e.seq)).toEqual(events.map((_, i) => i + 1));
    expect(events[0]).toMatchObject({
      type: "run_started",
      runId,
      fileName: input.name,
      table: input.table,
    });
    const finished = eventsOf(events, "run_finished");
    expect(finished).toHaveLength(1);
    expect(events.at(-1)?.type).toBe("run_finished");
    const final = finished[0];
    const plan = eventsOf(events, "plan_created")[0];
    const loaded = eventsOf(events, "loaded")[0];

    // The outcome badge and summary sentence come from `run_finished`.
    await expect(
      summary.getByText(STATUS_LABEL[final.status], { exact: true }),
    ).toBeVisible();
    await expect(summary.getByText(final.summary)).toBeVisible();

    // The sidebar settles on the final status with no reload: the list is
    // refetched when `run_finished` arrives.
    await expect(runLink).toHaveAttribute("aria-current", "page");
    await expect(runLink).toHaveAccessibleName(
      new RegExp(
        `^${escapeRegExp(input.name)}\\s*, ${STATUS_LABEL[final.status]}$`,
      ),
    );

    const timeline = await page.evaluate(
      () =>
        (window as unknown as { sluiceE2eTimeline: Timeline })
          .sluiceE2eTimeline,
    );

    if (!plan) {
      // A repeat of an already-loaded file: skipped, nothing run or written.
      test.info().annotations.push({
        type: "outcome",
        description: `skipped as already loaded (${final.summary})`,
      });
      expect(final.status).toBe("succeeded");
      expect(final.summary).toMatch(/already loaded/);
      expect(events.map((e) => e.type)).toEqual([
        "run_started",
        "run_finished",
      ]);
      await details.getByRole("tab", { name: "Data" }).click();
      await expect(
        details.getByRole("heading", { name: "Uploaded file (not loaded)" }),
      ).toBeVisible();
      expect((await scan(page)).blocking, "axe: run thread").toEqual([]);
    } else {
      test.info().annotations.push({
        type: "outcome",
        description: `fresh run, ${final.status}: ${final.summary}`,
      });
      // The pipeline did its job (a failure here means the run broke).
      expect(["succeeded", "quarantined"]).toContain(final.status);

      // Live, not a replay: the plan was on screen before the run finished.
      expect(timeline.planAt, "plan shown").not.toBeNull();
      expect(timeline.summaryAt, "summary shown").not.toBeNull();
      expect(timeline.planAt ?? 0).toBeLessThan(timeline.summaryAt ?? 0);

      // Code: each accepted transform, when the plan had nodes.
      const accepted = eventsOf(events, "node_finished").filter(
        (e) => e.tier !== "rules",
      );
      if (accepted.length > 0) {
        // Shown in the tab opened during the run (see above): the API served
        // each transform as its attempt ended.
        expect(timeline.codeAt, "accepted transform shown live").not.toBeNull();
        test.info().annotations.push({
          type: "code-tab",
          description: `first passed attempt shown ${Math.round(
            (timeline.codeAt ?? 0) - (timeline.summaryAt ?? 0),
          )} ms relative to the run summary`,
        });
        // Regression guard for live transforms: the accepted transform was
        // served while the run was in flight, before its summary appeared.
        expect(
          timeline.codeAt ?? Number.POSITIVE_INFINITY,
          "accepted transform shown before the run summary",
        ).toBeLessThan(timeline.summaryAt ?? 0);
        const transforms = await getTransforms(runId);
        for (const done of accepted) {
          const transform = transforms.find((t) => t.id === done.transformId);
          expect(transform, `transform ${done.transformId}`).toMatchObject({
            nodeId: done.nodeId,
            attempt: done.attempt,
            passed: true,
          });
          const title =
            plan.nodes.find((n) => n.id === done.nodeId)?.title ?? done.nodeId;
          const section = details.getByRole("region", {
            name: new RegExp(`${done.nodeId}\\s*${escapeRegExp(title)}`),
          });
          // The accepted attempt is listed as passed; show its code.
          const attempt = section.getByRole("radio", {
            // Chrome joins the label's inline parts with a space.
            name: new RegExp(`^Attempt ${done.attempt}\\s*, passed$`),
          });
          // The radio is visually hidden; its label is the click target.
          await attempt.locator("xpath=ancestor::label[1]").click();
          await expect(attempt).toBeChecked();
          const firstLine =
            transform?.code.split("\n").find((l) => l.trim() !== "") ?? "";
          await expect(
            section.getByRole("figure", {
              name: new RegExp(`^Attempt ${done.attempt} code`),
            }),
          ).toContainText(firstLine.trim());
        }
        expect((await scan(page)).blocking, "axe: Code tab").toEqual([]);
      }
      await details.getByRole("tab", { name: "Data" }).click();

      // Data: before/after rows once the run loaded.
      if (loaded) {
        const values = details.getByRole("table", {
          name: "Before and after values",
        });
        await expect(values).toBeVisible({ timeout: 30_000 });
        await expect(values.locator("tbody tr").first()).toBeVisible();
        await expect(
          values.getByRole("columnheader", { name: "before" }),
        ).toBeVisible();
        await expect(
          values.getByRole("columnheader", { name: "after" }),
        ).toBeVisible();
        // The UI retried until the preview was written; nothing else failed.
        expect(previews.at(-1)?.status).toBe(200);
        for (const p of previews.filter((r) => r.status !== 200)) {
          expect(p).toEqual({ status: 409, code: "not_ready" });
        }
      } else {
        await expect(
          details.getByRole("heading", { name: "Uploaded file (not loaded)" }),
        ).toBeVisible();
      }
      expect((await scan(page)).blocking, "axe: Data tab").toEqual([]);

      // Plan: one step per planned node.
      if (plan.nodes.length > 0) {
        await details.getByRole("tab", { name: "Plan" }).click();
        await expect(
          details
            .getByRole("region", { name: "Plan steps" })
            .getByRole("listitem"),
        ).toHaveCount(plan.nodes.length);
        expect((await scan(page)).blocking, "axe: Plan tab").toEqual([]);
      }
    }

    await details.getByRole("tab", { name: "Data" }).click();
    await page.screenshot({
      path: `test-results/screenshots/live-${CASE_ID}.png`,
      fullPage: true,
      animations: "disabled",
    });

    expect(problems).toEqual([]);
  });
});

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
