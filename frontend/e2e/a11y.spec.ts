import { expect, type Page, test } from "@playwright/test";
import {
  emulateReducedTransparency,
  openEvals,
  openFinishedRun,
  type Scan,
  scan,
} from "./helpers";

/*
 * Two renderings per theme:
 *
 * - "translucent": the glass as shipped. axe cannot resolve a background
 *   behind backdrop-filtered glass with a gradient ::before ring, so it
 *   reports text contrast as "incomplete" rather than checking it; this pass
 *   covers every other rule. The translucent colours themselves are guarded
 *   worst-case by tests/design/contrast.test.ts.
 * - "opaque": reduced transparency (the app's own solid-glass fallback, with
 *   no colour blobs) and reduced motion, plus the decorative ring hidden, so
 *   axe measures real, settled rendered contrast for every text node on glass.
 */
const MODES = ["translucent", "opaque"] as const;
type Mode = (typeof MODES)[number];

async function beforeLoad(page: Page, mode: Mode) {
  if (mode !== "opaque") return;
  await emulateReducedTransparency(page);
  // Measure settled colours: new thread content fades in from opacity 0
  // (.rise-in), and a scan mid-fade reads muted text as failing contrast.
  // The app's reduced-motion rule turns every animation off.
  await page.emulateMedia({ reducedMotion: "reduce" });
}

async function afterLoad(page: Page, mode: Mode) {
  if (mode !== "opaque") return;
  await page.addStyleTag({
    content: ".glass::before { display: none !important; }",
  });
}

/**
 * Most text axe still cannot measure with opaque glass, per view: the brand
 * link on the bare backdrop gradient (every view) and the diff's "−" marker
 * glyph (Code). The Plan tab is plain HTML cards now, so it is fully
 * measured. A regression that hides real text from the contrast check
 * pushes a view over its limit.
 */
const INCOMPLETE_LIMIT: Record<string, number> = {
  "Data tab": 1,
  "Live run": 1,
  "Code tab, diff": 2,
  "Plan tab": 1,
  Evals: 1,
};

function expectClean(result: Scan, mode: Mode, label: string) {
  expect(result.blocking, label).toEqual([]);
  if (mode !== "opaque") return;
  // Guard against a vacuous pass: the opaque rendering must really be measured.
  expect(result.contrastChecked, `${label}: contrast checked`).toBeGreaterThan(
    20,
  );
  expect(
    result.contrastIncomplete.length,
    `${label}: contrast not measurable\n${result.contrastIncomplete.join("\n")}`,
  ).toBeLessThanOrEqual(INCOMPLETE_LIMIT[label] ?? 0);
}

for (const colorScheme of ["light", "dark"] as const) {
  test.describe(`${colorScheme} theme`, () => {
    test.use({ colorScheme });

    for (const mode of MODES) {
      test(`${mode} glass: run thread and every details tab pass axe`, async ({
        page,
      }) => {
        await beforeLoad(page, mode);
        await openFinishedRun(page, "run-repaired");
        await afterLoad(page, mode);
        // System mode follows the emulated colour scheme.
        await expect(page.locator("html")).toHaveAttribute(
          "data-theme",
          "system",
        );
        expectClean(await scan(page), mode, "Data tab");

        const details = page.getByRole("complementary", { name: "Details" });
        await details.getByRole("tab", { name: "Code" }).click();
        await details
          .getByRole("switch", { name: "Diff vs previous attempt" })
          .click();
        await expect(
          details.getByRole("figure", { name: /^Changes from attempt/ }),
        ).toBeVisible();
        expectClean(await scan(page), mode, "Code tab, diff");

        await details.getByRole("tab", { name: "Plan" }).click();
        await expect(
          details.getByRole("region", { name: "Plan steps" }),
        ).toBeAttached();
        expectClean(await scan(page), mode, "Plan tab");
      });

      test(`${mode} glass: a live run's activity line and raw upload pass axe`, async ({
        page,
      }) => {
        await beforeLoad(page, mode);
        await page.goto("/runs");
        const composer = page.getByRole("form", { name: "New pipeline run" });
        await composer.getByLabel("Attach a file").setInputFiles({
          name: "orders.csv",
          mimeType: "text/csv",
          buffer: Buffer.from("o_orderkey,o_totalprice\n1,10.00\n"),
        });
        await composer.getByRole("button", { name: "Start run" }).click();
        await expect(page.getByTestId("activity-line")).toBeVisible({
          timeout: 15_000,
        });
        await expect(
          page
            .getByRole("complementary", { name: "Details" })
            .getByRole("table", { name: "Uploaded file" }),
        ).toBeVisible();
        await afterLoad(page, mode);
        expectClean(await scan(page), mode, "Live run");
      });

      test(`${mode} glass: evals page passes axe`, async ({ page }) => {
        await beforeLoad(page, mode);
        await openEvals(page);
        await afterLoad(page, mode);
        expectClean(await scan(page), mode, "Evals");
      });
    }

    test("full-page screenshot of the repaired run", async ({ page }) => {
      await openFinishedRun(page, "run-repaired");
      // CI artifact only: test-results/ is git-ignored.
      await page.screenshot({
        path: `test-results/screenshots/run-repaired-${colorScheme}.png`,
        fullPage: true,
        animations: "disabled",
      });
    });
  });
}

test("keyboard-only Tab order reaches the theme toggle, composer and tabs", async ({
  page,
}) => {
  await openFinishedRun(page, "run-repaired");
  await page.locator("body").focus();

  // Role and name of each focus stop, in Tab order.
  const visited: string[] = [];
  for (let i = 0; i < 60; i++) {
    await page.keyboard.press("Tab");
    const stop = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return "";
      const role = el.getAttribute("role") ?? el.tagName.toLowerCase();
      const name =
        el.getAttribute("aria-label") ?? el.textContent?.trim() ?? "";
      return `${role}:${name}`;
    });
    visited.push(stop);
    if (stop === "tab:Data") break;
  }
  const order = visited.join("\n");

  const themeToggle = visited.findIndex((s) => s.startsWith("button:Theme: "));
  const attach = visited.indexOf("input:Attach a file");
  const start = visited.indexOf("button:Start run");
  const tabs = visited.indexOf("tab:Data");

  // Reached, and in reading order: header, main column, then the side panel.
  expect(themeToggle, order).toBeGreaterThan(-1);
  expect(attach, order).toBeGreaterThan(themeToggle);
  expect(start, order).toBeGreaterThan(attach);
  expect(tabs, order).toBeGreaterThan(start);

  // Within the tab list, arrow keys move focus and Enter selects.
  const code = page.getByRole("tab", { name: "Code" });
  await page.keyboard.press("ArrowRight");
  await expect(code).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(code).toHaveAttribute("aria-selected", "true");

  // The theme toggle works from the keyboard too (system → light).
  await page.getByRole("button", { name: /^Theme: / }).focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("button", { name: "Theme: light" }),
  ).toBeFocused();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});
