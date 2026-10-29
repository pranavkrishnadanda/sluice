import type { RunSummary, Transform } from "../types";
import { buildEvents } from "./build";

/** lineitem file whose l_orderkey values partly miss orders: 212 quarantined. */
export const RUN_QUARANTINED: RunSummary = {
  id: "run-quarantined",
  fileName: "lineitem_2026-10.csv",
  table: "lineitem",
  rows: 60_175,
  status: "quarantined",
  createdAt: "2026-10-27T16:42:51.000Z",
  costUsd: 0.0009,
};

export const RUN_QUARANTINED_EVENTS = buildEvents(RUN_QUARANTINED.createdAt, [
  [
    0,
    {
      type: "run_started",
      runId: RUN_QUARANTINED.id,
      fileName: RUN_QUARANTINED.fileName,
      table: RUN_QUARANTINED.table,
      rows: RUN_QUARANTINED.rows,
    },
  ],
  [
    1_120,
    {
      type: "profiled",
      issues: [
        {
          column: "l_orderkey",
          kind: "foreign_key",
          detail: "212 rows reference orders that are not in the warehouse",
        },
        {
          column: "l_discount",
          kind: "unit",
          detail:
            'Discounts given as percentages (e.g. "5") instead of fractions',
        },
        {
          column: "l_shipmode",
          kind: "case",
          detail: 'Mixed-case enum values (e.g. "truck")',
        },
      ],
    },
  ],
  [
    520,
    {
      type: "rules_applied",
      actions: [
        "Upper-cased l_shipmode where that makes values valid",
        "Moved 212 rows with unknown l_orderkey to quarantine",
      ],
    },
  ],
  [
    980,
    {
      type: "plan_created",
      nodes: [
        {
          id: "n1",
          title: "Rescale l_discount percentages to fractions",
          columns: ["l_discount"],
          tier: "small",
          dependsOn: [],
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
    1_050,
    {
      type: "node_finished",
      nodeId: "n1",
      attempt: 1,
      tier: "small",
      tokens: 1_210,
      costUsd: 0.0009,
      latencyMs: 1_050,
      transformId: "t-quarantined-n1-1",
    },
  ],
  [700, { type: "verified", checksPassed: 12, checksTotal: 12 }],
  [
    2_310,
    {
      type: "loaded",
      rowsLoaded: 59_963,
      rowsQuarantined: 212,
      snapshotId: "snap-20261027-1642",
    },
  ],
  [
    60,
    {
      type: "run_finished",
      status: "quarantined",
      summary:
        "Loaded 59,963 rows; 212 rows with unknown l_orderkey are held in quarantine for review.",
      costUsd: 0.0009,
      tokens: 1_210,
    },
  ],
]);

export const RUN_QUARANTINED_TRANSFORMS: Transform[] = [
  {
    id: "t-quarantined-n1-1",
    nodeId: "n1",
    attempt: 1,
    language: "polars",
    passed: true,
    code: `import polars as pl

def transform(df: pl.DataFrame) -> pl.DataFrame:
    discount = pl.col("l_discount").cast(pl.Decimal(15, 4))
    return df.with_columns(
        pl.when(discount > 1).then(discount / 100).otherwise(discount)
        .cast(pl.Decimal(15, 2))
        .alias("l_discount")
    )
`,
  },
];
