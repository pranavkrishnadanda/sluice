import { describe, expect, test } from "vitest";
import { RUN_CLEAN, RUN_CLEAN_EVENTS } from "@/lib/api/fixtures/run-clean";
import {
  RUN_QUARANTINED,
  RUN_QUARANTINED_EVENTS,
} from "@/lib/api/fixtures/run-quarantined";
import {
  RUN_REPAIRED,
  RUN_REPAIRED_EVENTS,
} from "@/lib/api/fixtures/run-repaired";
import type { RunEvent, RunSummary } from "@/lib/api/types";

const FIXTURES: [string, RunSummary, readonly RunEvent[]][] = [
  [RUN_CLEAN.id, RUN_CLEAN, RUN_CLEAN_EVENTS],
  [RUN_REPAIRED.id, RUN_REPAIRED, RUN_REPAIRED_EVENTS],
  [RUN_QUARANTINED.id, RUN_QUARANTINED, RUN_QUARANTINED_EVENTS],
];

describe("fixture spend bookkeeping", () => {
  test.each(FIXTURES)(
    "%s: run_finished totals equal every attempt's spend",
    (_id, summary, events) => {
      let tokens = 0;
      let costUsd = 0;
      for (const e of events) {
        if (e.type === "node_finished" || e.type === "attempt_failed") {
          tokens += e.tokens;
          costUsd += e.costUsd;
        }
      }
      const finished = events.find((e) => e.type === "run_finished");
      if (finished?.type !== "run_finished") throw new Error("no run_finished");
      expect(finished.tokens).toBe(tokens);
      expect(finished.costUsd).toBeCloseTo(costUsd, 10);
      expect(summary.costUsd).toBeCloseTo(costUsd, 10);
    },
  );

  test("run-repaired's failed attempt carries its spend", () => {
    expect(
      RUN_REPAIRED_EVENTS.find((e) => e.type === "attempt_failed"),
    ).toMatchObject({ tokens: 1_480, costUsd: 0.0011 });
  });
});
