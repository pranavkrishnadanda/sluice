import { expect, type Page, test } from "@playwright/test";
import {
  emulateReducedTransparency,
  openEvals,
  openFinishedRun,
} from "./helpers";

const PHONE = { width: 390, height: 844 };

async function expectNoHorizontalScroll(page: Page) {
  const { scrollWidth, innerWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(innerWidth);
}

test.describe("phone width", () => {
  test.use({ viewport: PHONE });

  for (const path of ["/runs", "/evals"]) {
    test(`${path} has no horizontal page scroll`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expectNoHorizontalScroll(page);
    });
  }

  test("run page: no horizontal scroll, menu and Details drawers work", async ({
    page,
  }) => {
    await openFinishedRun(page, "run-repaired");
    await expectNoHorizontalScroll(page);

    const menu = page.getByRole("button", { name: "Menu" });
    await expect(menu).toBeVisible();
    await expect(menu).toHaveAttribute("aria-expanded", "false");

    const panel = page.getByRole("complementary", { name: "Details" });
    await expect(panel).toBeHidden();

    const details = page.getByRole("button", { name: "Details" });
    await details.click();
    await expect(details).toHaveAttribute("aria-expanded", "true");
    await expect(panel).toBeVisible();
    await expect(panel.getByRole("tab", { name: "Data" })).toBeVisible();
    await expectNoHorizontalScroll(page);

    // Escape closes the drawer and returns focus to its toggle.
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();
    await expect(details).toBeFocused();
  });
});

test.describe("reduced motion and transparency", () => {
  test.use({ reducedMotion: "reduce" });

  test("glass drops its backdrop blur and transitions", async ({ page }) => {
    await emulateReducedTransparency(page);
    await page.goto("/runs");
    await expect(page.locator("html")).toHaveAttribute(
      "data-reduce-transparency",
      "",
    );

    const form = page.getByRole("form", { name: "New pipeline run" });
    await expect(form).toBeVisible();
    // The composer is a `.glass` surface; read the computed glass styles.
    const style = await form.evaluate((el) => {
      const s = getComputedStyle(el);
      return {
        glass: el.classList.contains("glass"),
        backdropFilter: s.backdropFilter,
        transitionDuration: s.transitionDuration,
      };
    });
    expect(style.glass).toBe(true);
    expect(style.backdropFilter).toBe("none");
    expect(style.transitionDuration).toBe("0s");
  });
});

test.describe("desktop width", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("the results table fits its card without sideways scrolling", async ({
    page,
  }) => {
    await openEvals(page);
    const scroller = page.getByRole("region", {
      name: "Results by system, scrollable",
    });
    const { scrollWidth, clientWidth } = await scroller.evaluate((el) => ({
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
    }));
    expect(scrollWidth).toBeLessThanOrEqual(clientWidth);
  });

  // CI's Linux Chromium lays text out wider than macOS (glyph advances are
  // rounded, not fractional): the table once ran 9px past its card there.
  // Re-check the fit under deliberately wider text than any OS ships, so it
  // doesn't hinge on exact glyph widths: wide fallback fonts, and the real
  // fonts with 0.06em extra tracking (0.03em already reproduced CI's 673px).
  const STRESS: ReadonlyArray<{ name: string; css: string; check: string }> = [
    {
      name: "wide fallback fonts",
      css: `font-family: "DejaVu Sans", Verdana, sans-serif !important;`,
      check: "Verdana",
    },
    {
      name: "extra letter spacing",
      css: "letter-spacing: 0.06em !important;",
      check: "",
    },
  ];
  for (const { name, css, check } of STRESS) {
    test(`the results table still fits with ${name}`, async ({ page }) => {
      await openEvals(page);
      const region = '[aria-label="Results by system, scrollable"]';
      await page.addStyleTag({
        content: `${region} table, ${region} table * { ${css} }`,
      });
      const scroller = page.getByRole("region", {
        name: "Results by system, scrollable",
      });
      const { scrollWidth, clientWidth, family } = await scroller.evaluate(
        (el) => {
          const td = el.querySelector("td");
          return {
            scrollWidth: el.scrollWidth,
            clientWidth: el.clientWidth,
            family: td ? getComputedStyle(td).fontFamily : "",
          };
        },
      );
      expect(family).toContain(check);
      expect(scrollWidth).toBeLessThanOrEqual(clientWidth);
    });
  }
});
