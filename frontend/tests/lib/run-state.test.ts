import { describe, expect, test } from "vitest";
import type { RunEvent } from "@/lib/api/types";
import { type RunViewState, reduceRunEvents } from "@/lib/run-state";

const AT = "2026-10-28T09:00:00.000Z";

const started: RunEvent = {
  seq: 1,
  at: AT,
  type: "run_started",
  runId: "run-x",
  fileName: "orders.csv",
  table: "orders",
  rows: 100,
};

const plan: RunEvent = {
  seq: 2,
  at: AT,
  type: "plan_created",
  nodes: [
    {
      id: "n1",
      title: "Normalise amounts",
      columns: ["o_totalprice"],
      tier: "small",
      dependsOn: [],
    },
  ],
};

function reduceAll(events: RunEvent[]): RunViewState | undefined {
  return events.reduce<RunViewState | undefined>(reduceRunEvents, undefined);
}

describe("reduceRunEvents", () => {
  test("reducer ignores duplicate and stale seq", () => {
    const third: RunEvent = {
      seq: 3,
      at: AT,
      type: "node_started",
      nodeId: "n1",
      attempt: 1,
    };
    const once = reduceAll([started, plan, third]);
    const twice = reduceRunEvents(once, third);
    expect(twice).toEqual(once);
    expect(twice).toBe(once);

    const stale = reduceRunEvents(once, { ...plan, nodes: [] });
    expect(stale).toEqual(once);
    expect(once?.lastSeq).toBe(3);
  });

  test("reducer tracks retry then success", () => {
    const s1 = reduceAll([
      started,
      plan,
      { seq: 3, at: AT, type: "node_started", nodeId: "n1", attempt: 1 },
    ]);
    expect(s1?.nodes[0]).toMatchObject({ status: "running", attempt: 1 });

    const s2 = reduceRunEvents(s1, {
      seq: 4,
      at: AT,
      type: "attempt_failed",
      nodeId: "n1",
      attempt: 1,
      maxAttempts: 3,
      error: "contract check failed: 14 rows in paise",
    });
    expect(s2.nodes[0]).toMatchObject({
      status: "retrying",
      attempt: 1,
      maxAttempts: 3,
      lastError: "contract check failed: 14 rows in paise",
    });

    const s3 = reduceRunEvents(s2, {
      seq: 5,
      at: AT,
      type: "node_started",
      nodeId: "n1",
      attempt: 2,
    });
    expect(s3.nodes[0]).toMatchObject({
      status: "retrying",
      attempt: 2,
      maxAttempts: 3,
    });

    const s4 = reduceRunEvents(s3, {
      seq: 6,
      at: AT,
      type: "node_finished",
      nodeId: "n1",
      attempt: 2,
      tier: "strong",
      tokens: 1200,
      costUsd: 0.0186,
      latencyMs: 2140,
      transformId: "t-n1-2",
    });
    expect(s4.nodes[0]).toMatchObject({
      status: "done",
      attempt: 2,
      maxAttempts: 3,
      tier: "strong",
      costUsd: 0.0186,
      latencyMs: 2140,
      transformId: "t-n1-2",
    });
    expect(s4.totals).toMatchObject({ costUsd: 0.0186, tokens: 1200 });
    expect(s4.status).toBe("running");
    expect(s4.finished).toBe(false);
  });

  test("attempt_failed on the last attempt marks the node failed", () => {
    const s = reduceAll([
      started,
      plan,
      { seq: 3, at: AT, type: "node_started", nodeId: "n1", attempt: 3 },
      {
        seq: 4,
        at: AT,
        type: "attempt_failed",
        nodeId: "n1",
        attempt: 3,
        maxAttempts: 3,
        error: "boom",
      },
    ]);
    expect(s?.nodes[0]?.status).toBe("failed");
  });

  test("reducer marks noLlmWork for empty plan", () => {
    const s = reduceAll([
      started,
      { seq: 2, at: AT, type: "plan_created", nodes: [] },
    ]);
    expect(s?.noLlmWork).toBe(true);
    expect(s?.nodes).toEqual([]);

    const before = reduceAll([started]);
    expect(before?.noLlmWork).toBe(false);
    expect(reduceAll([started, plan])?.noLlmWork).toBe(false);
  });

  test("tracks the full run lifecycle", () => {
    const s = reduceAll([
      started,
      {
        seq: 2,
        at: AT,
        type: "profiled",
        issues: [{ column: "o_comment", kind: "whitespace", detail: "padded" }],
      },
      {
        seq: 3,
        at: AT,
        type: "rules_applied",
        actions: ["Stripped whitespace"],
      },
      { seq: 4, at: AT, type: "plan_created", nodes: [] },
      { seq: 5, at: AT, type: "verified", checksPassed: 12, checksTotal: 12 },
      {
        seq: 6,
        at: AT,
        type: "loaded",
        rowsLoaded: 100,
        rowsQuarantined: 0,
        snapshotId: "snap-1",
      },
      {
        seq: 7,
        at: AT,
        type: "run_finished",
        status: "succeeded",
        summary: "All good",
        costUsd: 0,
        tokens: 0,
      },
    ]);
    expect(s).toMatchObject({
      runId: "run-x",
      fileName: "orders.csv",
      table: "orders",
      rows: 100,
      status: "succeeded",
      issues: [{ column: "o_comment", kind: "whitespace" }],
      ruleActions: ["Stripped whitespace"],
      verified: { checksPassed: 12, checksTotal: 12 },
      loaded: { rowsLoaded: 100, rowsQuarantined: 0, snapshotId: "snap-1" },
      summary: "All good",
      finished: true,
      lastSeq: 7,
    });
  });

  test("starts queued and becomes running on run_started", () => {
    expect(reduceAll([started])?.status).toBe("running");
  });
});
