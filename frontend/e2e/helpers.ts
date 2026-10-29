import { AxeBuilder } from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/**
 * Waits for a run's replayed stream to finish: the last step ("Loaded …")
 * and the summary card that only renders once the run is finished.
 */
export async function waitForFinishedRun(page: Page) {
  await expect(page.getByText(/^Loaded [\d,]+ rows/)).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText(/^Total cost /)).toBeVisible();
}

/** Opens a run and waits until its replayed stream has finished. */
export async function openFinishedRun(page: Page, runId: string) {
  await page.goto(`/runs/${runId}`);
  await waitForFinishedRun(page);
}

/** Opens the evals page and waits for the results table and the chart. */
export async function openEvals(page: Page) {
  await page.goto("/evals");
  await expect(
    page.getByRole("region", { name: "Results by system, scrollable" }),
  ).toBeVisible();
  await expect(page.getByRole("status", { name: "Loading chart" })).toHaveCount(
    0,
  );
}

/**
 * Makes `prefers-reduced-transparency: reduce` match. Chromium can't emulate
 * that media feature, so this stubs `matchMedia` before any page script runs;
 * the app's own head script and ThemeProvider then set the
 * `html[data-reduce-transparency]` hook from it.
 */
export async function emulateReducedTransparency(page: Page) {
  await page.addInitScript(() => {
    const real = window.matchMedia.bind(window);
    window.matchMedia = (query: string) =>
      query.includes("prefers-reduced-transparency: reduce")
        ? real("all")
        : real(query);
  });
}

const TAGS = [
  "wcag2a",
  "wcag2aa",
  "wcag21a",
  "wcag21aa",
  "wcag22aa",
  "best-practice",
];

/**
 * Rules that fail at any impact. Everything else fails only when serious or
 * critical. `heading-order` (moderate, best-practice) guards the panel's
 * heading levels under the run's h1.
 */
const ALWAYS_BLOCKING = new Set(["heading-order"]);

export interface Scan {
  /** Blocking violations, summarised for a readable diff. */
  blocking: { id: string; impact: string; help: string; targets: string[] }[];
  /** Text nodes whose contrast axe actually measured (and passed). */
  contrastChecked: number;
  /** Text nodes axe could not measure, with axe's reason. */
  contrastIncomplete: string[];
}

/** Runs axe over the page (see `a11y.spec.ts` for how results are judged). */
export async function scan(page: Page): Promise<Scan> {
  const { violations, passes, incomplete } = await new AxeBuilder({ page })
    .withTags(TAGS)
    .analyze();
  return {
    blocking: violations
      .filter(
        (v) =>
          v.impact === "serious" ||
          v.impact === "critical" ||
          ALWAYS_BLOCKING.has(v.id),
      )
      .map((v) => ({
        id: v.id,
        impact: v.impact ?? "",
        help: v.help,
        targets: v.nodes.map((n) => n.target.join(" ")),
      })),
    contrastChecked:
      passes.find((p) => p.id === "color-contrast")?.nodes.length ?? 0,
    contrastIncomplete:
      incomplete
        .find((p) => p.id === "color-contrast")
        ?.nodes.map(
          (n) =>
            `${n.target.join(" ")}: ${n.any.map((c) => c.message).join("; ")}`,
        ) ?? [],
  };
}
