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
