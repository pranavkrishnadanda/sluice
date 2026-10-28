import { AxeBuilder } from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import {
  emulateReducedTransparency,
  openEvals,
  openFinishedRun,
} from "./helpers";

const WCAG_AA = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

interface Scan {
  /** Serious or critical WCAG A/AA violations, summarised for a readable diff. */
  blocking: { id: string; impact: string; help: string; targets: string[] }[];
  /** Text nodes whose contrast axe actually measured (and passed). */
  contrastChecked: number;
}

async function scan(page: Page): Promise<Scan> {
  const { violations, passes } = await new AxeBuilder({ page })
    .withTags(WCAG_AA)
    .analyze();
  return {
    blocking: violations
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .map((v) => ({
        id: v.id,
        impact: v.impact ?? "",
        help: v.help,
        targets: v.nodes.map((n) => n.target.join(" ")),
      })),
    contrastChecked:
      passes.find((p) => p.id === "color-contrast")?.nodes.length ?? 0,
  };
}

/*
 * Two renderings per theme:
 *
 * - "translucent": the glass as shipped. axe cannot resolve a background
 *   behind backdrop-filtered glass with a gradient ::before ring, so it
 *   reports text contrast as "incomplete" rather than checking it; this pass
 *   covers every other rule. The translucent colours themselves are guarded
 *   worst-case by tests/design/contrast.test.ts.
 * - "opaque": reduced transparency (the app's own solid-glass fallback) with
 *   the decorative ring hidden, so axe measures real rendered contrast for
 *   every text node on glass.
 */
const MODES = ["translucent", "opaque"] as const;
type Mode = (typeof MODES)[number];

async function beforeLoad(page: Page, mode: Mode) {
  if (mode === "opaque") await emulateReducedTransparency(page);
}

async function afterLoad(page: Page, mode: Mode) {
  if (mode !== "opaque") return;
  await page.addStyleTag({
    content: ".glass::before { display: none !important; }",
  });
}

function expectClean(result: Scan, mode: Mode, label: string) {
  expect(result.blocking, label).toEqual([]);
  // Guard against a vacuous pass: the opaque rendering must really be measured.
  if (mode === "opaque") {
    expect(
      result.contrastChecked,
      `${label}: contrast checked`,
    ).toBeGreaterThan(20);
  }
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
          details.getByRole("list", { name: "Plan steps" }),
        ).toBeAttached();
        expectClean(await scan(page), mode, "Plan tab");
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
