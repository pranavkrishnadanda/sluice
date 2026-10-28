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
      maxAttempts: 3,
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
      {
        seq: 3,
        at: AT,
        type: "node_started",
        nodeId: "n1",
        attempt: 1,
        maxAttempts: 3,
      },
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
      tokens: 0,
      costUsd: 0,
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
      maxAttempts: 3,
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

  test("attemptsReported counts finished or failed attempts, not starts", () => {
    const s1 = reduceAll([
      started,
      plan,
      {
        seq: 3,
        at: AT,
        type: "node_started",
        nodeId: "n1",
        attempt: 1,
        maxAttempts: 3,
      },
    ]);
    expect(s1?.nodes[0]?.attemptsReported).toBe(0);
    const s2 = reduceRunEvents(s1, {
      seq: 4,
      at: AT,
      type: "attempt_failed",
      nodeId: "n1",
      attempt: 1,
      maxAttempts: 3,
      error: "x",
      tokens: 0,
      costUsd: 0,
    });
    expect(s2.nodes[0]?.attemptsReported).toBe(1);
    const s3 = reduceRunEvents(s2, {
      seq: 5,
      at: AT,
      type: "node_started",
      nodeId: "n1",
      attempt: 2,
      maxAttempts: 3,
    });
    expect(s3.nodes[0]?.attemptsReported).toBe(1);
    const s4 = reduceRunEvents(s3, {
      seq: 6,
      at: AT,
      type: "node_finished",
      nodeId: "n1",
      attempt: 2,
      tier: "strong",
      tokens: 0,
      costUsd: 0,
      latencyMs: 1,
      transformId: "t-n1-2",
    });
    expect(s4.nodes[0]?.attemptsReported).toBe(2);
  });

  test("a node's spend sums every attempt, failed ones included", () => {
    const s = reduceAll([
      started,
      plan,
      {
        seq: 3,
        at: AT,
        type: "node_started",
        nodeId: "n1",
        attempt: 1,
        maxAttempts: 3,
      },
      {
        seq: 4,
        at: AT,
        type: "attempt_failed",
        nodeId: "n1",
        attempt: 1,
        maxAttempts: 3,
        error: "contract check failed: 14 rows in paise",
        tokens: 1480,
        costUsd: 0.0011,
      },
    ]);
    expect(s?.nodes[0]).toMatchObject({ tokens: 1480, costUsd: 0.0011 });
    expect(s?.totals).toMatchObject({ tokens: 1480, costUsd: 0.0011 });

    const done = reduceAll([
      ...[started, plan],
      {
        seq: 3,
        at: AT,
        type: "node_started",
        nodeId: "n1",
        attempt: 1,
        maxAttempts: 3,
      },
      {
        seq: 4,
        at: AT,
        type: "attempt_failed",
        nodeId: "n1",
        attempt: 1,
        maxAttempts: 3,
        error: "contract check failed: 14 rows in paise",
        tokens: 1480,
        costUsd: 0.0011,
      },
      {
        seq: 5,
        at: AT,
        type: "node_started",
        nodeId: "n1",
        attempt: 2,
        maxAttempts: 3,
      },
      {
        seq: 6,
        at: AT,
        type: "node_finished",
        nodeId: "n1",
        attempt: 2,
        tier: "strong",
        tokens: 3120,
        costUsd: 0.0186,
        latencyMs: 2140,
        transformId: "t-n1-2",
      },
    ]);
    expect(done?.nodes[0]?.tokens).toBe(4600);
    expect(done?.nodes[0]?.costUsd).toBeCloseTo(0.0197, 10);
    expect(done?.totals.tokens).toBe(4600);
    expect(done?.totals.costUsd).toBeCloseTo(0.0197, 10);
  });

  test("attempt_failed on the last attempt marks the node failed", () => {
    const s = reduceAll([
      started,
      plan,
      {
        seq: 3,
        at: AT,
        type: "node_started",
        nodeId: "n1",
        attempt: 3,
        maxAttempts: 3,
      },
      {
        seq: 4,
        at: AT,
        type: "attempt_failed",
        nodeId: "n1",
        attempt: 3,
        maxAttempts: 3,
        error: "boom",
        tokens: 0,
        costUsd: 0,
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

  test("flags profiling and rules even when they find nothing", () => {
    const s0 = reduceAll([started]);
    expect(s0).toMatchObject({ profiled: false, rulesApplied: false });
    const s = reduceAll([
      started,
      { seq: 2, at: AT, type: "profiled", issues: [] },
      { seq: 3, at: AT, type: "rules_applied", actions: [] },
    ]);
    expect(s).toMatchObject({
      profiled: true,
      rulesApplied: true,
      issues: [],
      ruleActions: [],
    });
  });
  describe("early end: run_finished closes every open node", () => {
    const twoNodePlan: RunEvent = {
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
        {
          id: "n2",
          title: "Parse dates",
          columns: ["o_orderdate"],
          tier: "small",
          dependsOn: [],
        },
        {
          id: "n3",
          title: "Join customers",
          columns: ["o_custkey"],
          tier: "strong",
          dependsOn: ["n1", "n2"],
        },
      ],
    };

    const finished = (
      seq: number,
      status: "succeeded" | "quarantined" | "failed",
      costUsd: number,
      tokens: number,
    ): RunEvent => ({
      seq,
      at: "2026-10-28T09:05:00.000Z",
      type: "run_finished",
      status,
      summary: "ended",
      costUsd,
      tokens,
    });

    test("quarantined early end closes a node stopped mid-attempt", () => {
      const s = reduceAll([
        started,
        twoNodePlan,
        {
          seq: 3,
          at: AT,
          type: "node_started",
          nodeId: "n1",
          attempt: 1,
          maxAttempts: 3,
        },
        {
          seq: 4,
          at: AT,
          type: "node_started",
          nodeId: "n2",
          attempt: 3,
          maxAttempts: 3,
        },
        {
          seq: 5,
          at: AT,
          type: "attempt_failed",
          nodeId: "n2",
          attempt: 3,
          maxAttempts: 3,
          error: "contract check failed",
          tokens: 10,
          costUsd: 0.01,
        },
        finished(6, "quarantined", 0.05, 50),
      ]);
      expect(s?.status).toBe("quarantined");
      const [n1, n2, n3] = s?.nodes ?? [];
      expect(n1).toMatchObject({ status: "failed", attempt: 1 });
      expect(n1?.lastError).toMatch(/run ended/i);
      expect(n2).toMatchObject({
        status: "failed",
        lastError: "contract check failed",
      });
      // A node that never started is not open; it stays pending.
      expect(n3?.status).toBe("pending");
      expect(
        s?.nodes.some((n) => n.status === "running" || n.status === "retrying"),
      ).toBe(false);
    });

    test("a retrying node whose last attempt_failed had no follow-up keeps its error", () => {
      const s = reduceAll([
        started,
        twoNodePlan,
        {
          seq: 3,
          at: AT,
          type: "node_started",
          nodeId: "n1",
          attempt: 1,
          maxAttempts: 3,
        },
        {
          seq: 4,
          at: AT,
          type: "attempt_failed",
          nodeId: "n1",
          attempt: 1,
          maxAttempts: 3,
          error: "provider timeout",
          tokens: 0,
          costUsd: 0,
        },
        finished(5, "failed", 0, 0),
      ]);
      expect(s?.nodes[0]).toMatchObject({
        status: "failed",
        attempt: 1,
        lastError: "provider timeout",
      });
    });

    test("a succeeded run_finished arriving while a node is open still closes it", () => {
      const s = reduceAll([
        started,
        twoNodePlan,
        {
          seq: 3,
          at: AT,
          type: "node_started",
          nodeId: "n1",
          attempt: 2,
          maxAttempts: 3,
        },
        finished(4, "succeeded", 0, 0),
      ]);
      expect(s?.status).toBe("succeeded");
      expect(s?.finished).toBe(true);
      expect(s?.nodes[0]?.status).toBe("failed");
      expect(s?.nodes[1]?.status).toBe("pending");
    });

    test("interleaved events of concurrent nodes are tracked per node", () => {
      const s = reduceAll([
        started,
        twoNodePlan,
        {
          seq: 3,
          at: AT,
          type: "node_started",
          nodeId: "n1",
          attempt: 1,
          maxAttempts: 3,
        },
        {
          seq: 4,
          at: AT,
          type: "node_started",
          nodeId: "n2",
          attempt: 1,
          maxAttempts: 2,
        },
        {
          seq: 5,
          at: AT,
          type: "attempt_failed",
          nodeId: "n1",
          attempt: 1,
          maxAttempts: 3,
          error: "bad cast",
          tokens: 5,
          costUsd: 0.001,
        },
        {
          seq: 6,
          at: AT,
          type: "node_finished",
          nodeId: "n2",
          attempt: 1,
          tier: "small",
          tokens: 7,
          costUsd: 0.002,
          latencyMs: 300,
          transformId: "t-n2-1",
        },
        {
          seq: 7,
          at: AT,
          type: "node_started",
          nodeId: "n1",
          attempt: 2,
          maxAttempts: 3,
        },
      ]);
      const [n1, n2] = s?.nodes ?? [];
      expect(n1).toMatchObject({
        status: "retrying",
        attempt: 2,
        attemptsReported: 1,
        maxAttempts: 3,
        lastError: "bad cast",
        tokens: 5,
      });
      expect(n2).toMatchObject({
        status: "done",
        attempt: 1,
        maxAttempts: 2,
        lastError: null,
        tokens: 7,
        transformId: "t-n2-1",
      });
      expect(s?.totals).toEqual({ costUsd: 0.003, tokens: 12, latencyMs: 300 });

      const end = reduceRunEvents(s, finished(8, "failed", 0.004, 20));
      expect(end.nodes[0]).toMatchObject({ status: "failed", attempt: 2 });
      expect(end.nodes[0]?.lastError).toMatch(/run ended/i);
      expect(end.nodes[1]?.status).toBe("done");
    });

    test("totals jump to run_finished's figures even when above the node sum", () => {
      const before = reduceAll([
        started,
        twoNodePlan,
        {
          seq: 3,
          at: AT,
          type: "node_started",
          nodeId: "n1",
          attempt: 1,
          maxAttempts: 3,
        },
        {
          seq: 4,
          at: AT,
          type: "node_finished",
          nodeId: "n1",
          attempt: 1,
          tier: "small",
          tokens: 100,
          costUsd: 0.01,
          latencyMs: 250,
          transformId: "t-n1-1",
        },
        {
          seq: 5,
          at: AT,
          type: "node_started",
          nodeId: "n2",
          attempt: 1,
          maxAttempts: 3,
        },
      ]);
      expect(before?.totals).toEqual({
        costUsd: 0.01,
        tokens: 100,
        latencyMs: 250,
      });
      const end = reduceRunEvents(
        before,
        finished(6, "quarantined", 0.42, 4200),
      );
      expect(end.totals).toEqual({
        costUsd: 0.42,
        tokens: 4200,
        latencyMs: 250,
      });
      expect(end.finishedAt).toBe("2026-10-28T09:05:00.000Z");
    });
  });
});
