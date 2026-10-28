import { describe, expect, test } from "vitest";
import {
  formatCI,
  formatCount,
  formatMs,
  formatPct,
  formatUsd,
} from "@/lib/format";

describe("format", () => {
  test("formatCount uses Indian grouping", () => {
    expect(formatCount(124_500)).toBe("1,24,500");
    expect(formatCount(48_210)).toBe("48,210");
    expect(formatCount(0)).toBe("0");
  });

  test("formatUsd / formatMs / formatPct / formatCI", () => {
    expect(formatUsd(0)).toBe("$0");
    expect(formatUsd(0.0031)).toBe("$0.0031");
    expect(formatUsd(1.5)).toBe("$1.50");

    expect(formatMs(820)).toBe("820 ms");
    expect(formatMs(1400)).toBe("1.4 s");

    expect(formatPct(0.814)).toBe("81.4%");

    expect(formatCI({ mean: 0.814, lo: 0.79, hi: 0.836 })).toBe(
      "81.4% [79.0, 83.6]",
    );
    expect(formatCI(null)).toBe("—");
  });

  test("formatCI can format latency intervals", () => {
    expect(formatCI({ mean: 1_120, lo: 940, hi: 1_310 }, "ms")).toBe(
      "1.1 s [940 ms, 1.3 s]",
    );
  });

  test("formatCI can format USD intervals", () => {
    expect(formatCI({ mean: 0.12, lo: 0.1, hi: 0.15 }, "usd")).toBe(
      "$0.12 [$0.10, $0.15]",
    );
  });
});
