import { describe, expect, test } from "vitest";
import { rowsCaption } from "@/components/panel/DataTab";

describe("Data tab caption", () => {
  test("says rows shown of the upload, then rows loaded", () => {
    expect(rowsCaption(1_000, 41_200, true, false, 40_000)).toBe(
      "Showing 1,000 of 41,200 uploaded rows · 40,000 loaded",
    );
  });

  test("every row shown", () => {
    expect(rowsCaption(12, 12, false, false, 12)).toBe(
      "Showing all 12 uploaded rows · 12 loaded",
    );
  });

  test("no load count yet, and sample runs never claim one", () => {
    expect(rowsCaption(5, 10, true, false)).toBe(
      "Showing 5 of 10 uploaded rows",
    );
    expect(rowsCaption(5, 10, true, true, 10)).toBe(
      "Sample run · showing 5 of 10 sample rows",
    );
  });
});
