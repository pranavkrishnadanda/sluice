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
});
