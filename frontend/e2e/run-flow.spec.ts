import { expect, test } from "@playwright/test";
import { waitForFinishedRun } from "./helpers";

const CSV = [
  "o_orderkey,o_custkey,o_orderstatus,o_totalprice,o_orderdate",
  "1,36901,O,173665.47,1996-01-02",
  "2,78002,O,46929.18,12/01/1996",
  "3,123314,F,193846.25,1993-10-14",
].join("\n");

test("a new upload streams to completion and exposes its code and plan", async ({
  page,
}) => {
  await page.goto("/runs");
  await expect(
    page.getByRole("heading", { level: 1, name: "New pipeline run" }),
  ).toBeVisible();
  await expect(page.getByText("Demo data")).toBeVisible();

  const composer = page.getByRole("form", { name: "New pipeline run" });
  await composer.getByLabel("Attach a file").setInputFiles({
    name: "orders-drifted.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(CSV),
  });
  await expect(composer.getByText("orders-drifted.csv")).toBeVisible();
  await composer.getByRole("button", { name: "Start run" }).click();

  // The mock numbers created runs from 1 and replays the repaired fixture.
  await expect(page).toHaveURL(/\/runs\/run-1$/);
  await waitForFinishedRun(page);

  const details = page.getByRole("complementary", { name: "Details" });

  await details.getByRole("tab", { name: "Code" }).click();
  const diff = details.getByRole("switch", {
    name: "Diff vs previous attempt",
  });
  await expect(diff).toHaveAttribute("aria-checked", "false");
  await diff.click();
  await expect(diff).toHaveAttribute("aria-checked", "true");
  await expect(
    details.getByRole("figure", {
      name: /^Changes from attempt 1 to attempt 2$/,
    }),
  ).toBeVisible();

  await details.getByRole("tab", { name: "Plan" }).click();
  await expect(
    details.getByRole("list", { name: "Plan steps" }).getByRole("listitem"),
  ).toHaveCount(2);
});
