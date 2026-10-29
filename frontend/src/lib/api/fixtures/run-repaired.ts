import type { RunSummary, Transform } from "../types";
import { buildEvents } from "./build";

/*
 * Cost/token bookkeeping (kept consistent on purpose):
 *   n1 attempt 1 (failed)   small   1,480 tok  $0.0011
 *   n1 attempt 2 (passed)   strong  3,120 tok  $0.0186
 *   n2 attempt 1 (passed)   small     940 tok  $0.0007
 *   run total                       5,540 tok  $0.0204
 */
export const RUN_REPAIRED: RunSummary = {
  id: "run-repaired",
  fileName: "orders_2026-10.csv",
  table: "orders",
  rows: 48_210,
  status: "succeeded",
  createdAt: "2026-10-27T10:14:03.000Z",
  costUsd: 0.0204,
};

export const RUN_REPAIRED_EVENTS = buildEvents(RUN_REPAIRED.createdAt, [
  [
    0,
    {
      type: "run_started",
      runId: RUN_REPAIRED.id,
      fileName: RUN_REPAIRED.fileName,
      table: RUN_REPAIRED.table,
      rows: RUN_REPAIRED.rows,
    },
  ],
  [
    910,
    {
      type: "profiled",
      issues: [
        {
          column: "amt",
          kind: "rename",
          detail: "No contract column named amt; values match o_totalprice",
        },
        {
          column: "amt",
          kind: "format",
          detail:
            'Indian digit grouping in amount strings (e.g. "1,24,500.50")',
        },
        {
          column: "amt",
          kind: "unit",
          detail: "Some amounts look like paise (100× rupee scale)",
        },
        {
          column: "o_comment",
          kind: "whitespace",
          detail: "2,904 values have leading or trailing spaces",
        },
      ],
    },
  ],
  [
    450,
    {
      type: "rules_applied",
      actions: [
        "Stripped whitespace in o_comment",
        "Parsed o_orderdate as ISO-8601",
        "Left amt unchanged: Indian grouping and paise are not safe to guess",
      ],
    },
  ],
  [
    1_260,
    {
      type: "plan_created",
      nodes: [
        {
          id: "n1",
          title: "Map amt to o_totalprice and normalise amounts",
          columns: ["amt", "o_totalprice"],
          tier: "small",
          dependsOn: [],
        },
        {
          id: "n2",
          title: "Flag implausible o_totalprice values",
          columns: ["o_totalprice"],
          tier: "small",
          dependsOn: ["n1"],
        },
      ],
    },
  ],
  [
    40,
    {
      type: "node_started",
      nodeId: "n1",
      attempt: 1,
      maxAttempts: 3,
      tier: "small",
    },
  ],
  [
    1_380,
    {
      type: "attempt_failed",
      nodeId: "n1",
      attempt: 1,
      maxAttempts: 3,
      error: "contract check failed: 14 rows in paise",
      tokens: 1_480,
      costUsd: 0.0011,
    },
  ],
  [
    30,
    {
      type: "node_started",
      nodeId: "n1",
      attempt: 2,
      maxAttempts: 3,
      tier: "strong",
    },
  ],
  [
    2_140,
    {
      type: "node_finished",
      nodeId: "n1",
      attempt: 2,
      tier: "strong",
      tokens: 3_120,
      costUsd: 0.0186,
      latencyMs: 2_140,
      transformId: "t-repaired-n1-2",
    },
  ],
  [
    30,
    {
      type: "node_started",
      nodeId: "n2",
      attempt: 1,
      maxAttempts: 3,
      tier: "small",
    },
  ],
  [
    820,
    {
      type: "node_finished",
      nodeId: "n2",
      attempt: 1,
      tier: "small",
      tokens: 940,
      costUsd: 0.0007,
      latencyMs: 820,
      transformId: "t-repaired-n2-1",
    },
  ],
  [610, { type: "verified", checksPassed: 12, checksTotal: 12 }],
  [
    1_740,
    {
      type: "loaded",
      rowsLoaded: 48_210,
      rowsQuarantined: 0,
      snapshotId: "snap-20261027-1014",
    },
  ],
  [
    60,
    {
      type: "run_finished",
      status: "succeeded",
      summary:
        "Renamed amt to o_totalprice and normalised Indian-grouped and paise amounts after one retry; all 12 contract checks passed.",
      costUsd: 0.0204,
      tokens: 5_540,
    },
  ],
]);

export const RUN_REPAIRED_TRANSFORMS: Transform[] = [
  {
    id: "t-repaired-n1-1",
    nodeId: "n1",
    attempt: 1,
    language: "polars",
    passed: false,
    code: `import polars as pl

def transform(df: pl.DataFrame) -> pl.DataFrame:
    return df.rename({"amt": "o_totalprice"}).with_columns(
        pl.col("o_totalprice")
        .str.replace_all(",", "")
        .cast(pl.Decimal(15, 2))
    )
`,
  },
  {
    id: "t-repaired-n1-2",
    nodeId: "n1",
    attempt: 2,
    language: "polars",
    passed: true,
    code: `import polars as pl

# Paise values are integer strings with no separator and a
# 100x scale versus the order's line items.
PAISE = pl.col("o_totalprice").str.contains(r"^\\d{7,}$")

def transform(df: pl.DataFrame) -> pl.DataFrame:
    amount = (
        pl.col("o_totalprice")
        .str.replace_all(",", "")
        .cast(pl.Decimal(18, 2))
    )
    return df.rename({"amt": "o_totalprice"}).with_columns(
        pl.when(PAISE).then(amount / 100).otherwise(amount)
        .cast(pl.Decimal(15, 2))
        .alias("o_totalprice")
    )
`,
  },
  {
    id: "t-repaired-n2-1",
    nodeId: "n2",
    attempt: 1,
    language: "sql",
    passed: true,
    code: `SELECT
  o_orderkey,
  o_totalprice,
  o_totalprice <= 0 OR o_totalprice > 600000 AS flagged
FROM orders_staging;
`,
  },
];
