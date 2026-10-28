import type { RunSummary, Transform } from "../types";
import { buildEvents } from "./build";

/** Easy file: whitespace + d/m/y dates. Rules fix everything; no LLM work. */
export const RUN_CLEAN: RunSummary = {
  id: "run-clean",
  fileName: "orders_2026-09.csv",
  table: "orders",
  rows: 15_000,
  status: "succeeded",
  createdAt: "2026-10-26T08:30:12.000Z",
  costUsd: 0,
};

export const RUN_CLEAN_EVENTS = buildEvents(RUN_CLEAN.createdAt, [
  [
    0,
    {
      type: "run_started",
      runId: RUN_CLEAN.id,
      fileName: RUN_CLEAN.fileName,
      table: RUN_CLEAN.table,
      rows: RUN_CLEAN.rows,
    },
  ],
  [
    640,
    {
      type: "profiled",
      issues: [
        {
          column: "o_clerk",
          kind: "whitespace",
          detail: "1,212 values have leading or trailing spaces",
        },
        {
          column: "o_comment",
          kind: "whitespace",
          detail: "3,486 values have leading or trailing spaces",
        },
        {
          column: "o_orderdate",
          kind: "date_format",
          detail:
            'Dates are day-first (e.g. "14/09/2026"); contract is ISO-8601',
        },
      ],
    },
  ],
  [
    410,
    {
      type: "rules_applied",
      actions: [
        "Stripped whitespace in o_clerk, o_comment",
        "Parsed o_orderdate as %d/%m/%Y (contract locale en_IN)",
      ],
    },
  ],
  [120, { type: "plan_created", nodes: [] }],
  [380, { type: "verified", checksPassed: 12, checksTotal: 12 }],
  [
    520,
    {
      type: "loaded",
      rowsLoaded: 15_000,
      rowsQuarantined: 0,
      snapshotId: "snap-20261026-0830",
    },
  ],
  [
    60,
    {
      type: "run_finished",
      status: "succeeded",
      summary:
        "Deterministic rules repaired every issue; no model calls were needed.",
      costUsd: 0,
      tokens: 0,
    },
  ],
]);

export const RUN_CLEAN_TRANSFORMS: Transform[] = [];
