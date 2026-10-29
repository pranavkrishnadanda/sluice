import { describe, expect, test } from "vitest";
import type { RunEvent } from "@/lib/api/types";
import {
  latestActivity,
  type RunViewState,
  reduceRunEvents,
} from "@/lib/run-state";

const AT = "2026-10-28T09:00:00.000Z";

type Body = RunEvent extends infer E
  ? E extends RunEvent
    ? Omit<E, "seq" | "at">
    : never
  : never;

/** Numbers events from 1 and stamps them all at `AT`. */
function run(...bodies: Body[]): RunViewState | undefined {
  return bodies
    .map((b, i) => ({ ...b, seq: i + 1, at: AT }) as RunEvent)
    .reduce<RunViewState | undefined>(reduceRunEvents, undefined);
}

const started: Body = {
  type: "run_started",
  runId: "run-x",
  fileName: "orders.csv",
  table: "orders",
  rows: 100,
};
const plan: Body = {
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
const activity = (
  over: Partial<Extract<Body, { type: "activity" }>> = {},
): Body => ({
  type: "activity",
  nodeId: null,
  phase: "planning",
  message: "Planning the repairs",
  tier: null,
  attempt: null,
  retryInS: null,
  ...over,
});
const nodeStarted = (attempt: number, tier?: "small" | "strong"): Body => ({
  type: "node_started",
  nodeId: "n1",
  attempt,
  maxAttempts: 3,
  ...(tier ? { tier } : {}),
});

describe("activity", () => {
  test("a run-wide activity is kept with its seq and start time", () => {
    const s = run(started, activity());
    expect(s?.activity).toEqual({
      seq: 2,
      at: AT,
      phase: "planning",
      message: "Planning the repairs",
      tier: null,
      attempt: null,
      retryInS: null,
    });
  });

  test("a later activity replaces the run-wide one", () => {
    const s = run(
      started,
      activity({ phase: "mapping", message: "Mapping columns" }),
      activity(),
    );
    expect(s?.activity?.phase).toBe("planning");
  });

  test("any later non-activity event clears the run-wide activity", () => {
    expect(run(started, activity(), plan)?.activity).toBeNull();
  });

  test("a node activity lands on its node and clears the run-wide one", () => {
    const s = run(
      started,
      activity(),
      plan,
      nodeStarted(1),
      activity({
        nodeId: "n1",
        phase: "writing",
        message: "Writing",
        attempt: 1,
      }),
    );
    expect(s?.activity).toBeNull();
    expect(s?.nodes[0]?.activity?.phase).toBe("writing");
  });

  test("an activity for an unknown node is ignored", () => {
    const s = run(started, plan, activity({ nodeId: "zz", phase: "writing" }));
    expect(s?.nodes[0]?.activity).toBeNull();
    expect(s?.activity).toBeNull();
  });

  test("attempt_failed and node_finished clear the node's activity", () => {
    const writing = activity({ nodeId: "n1", phase: "writing", attempt: 1 });
    const failed = run(started, plan, nodeStarted(1), writing, {
      type: "attempt_failed",
      nodeId: "n1",
      attempt: 1,
      maxAttempts: 3,
      error: "boom",
      tokens: 1,
      costUsd: 0,
    });
    expect(failed?.nodes[0]?.activity).toBeNull();
    const done = run(started, plan, nodeStarted(1), writing, {
      type: "node_finished",
      nodeId: "n1",
      attempt: 1,
      tier: "small",
      tokens: 1,
      costUsd: 0,
      latencyMs: 5,
      transformId: "t1",
    });
    expect(done?.nodes[0]?.activity).toBeNull();
  });

  test("node_started keeps an activity of the same attempt, drops an older one", () => {
    const early = run(
      started,
      plan,
      activity({ nodeId: "n1", phase: "writing", attempt: 2 }),
      nodeStarted(2),
    );
    expect(early?.nodes[0]?.activity?.phase).toBe("writing");
    const stale = run(
      started,
      plan,
      nodeStarted(1),
      activity({ nodeId: "n1", phase: "sandbox", attempt: 1 }),
      nodeStarted(2),
    );
    expect(stale?.nodes[0]?.activity).toBeNull();
  });

  test("a waiting activity keeps retryInS", () => {
    const s = run(
      started,
      plan,
      nodeStarted(1),
      activity({
        nodeId: "n1",
        phase: "waiting",
        message: "Rate limit reached — retrying in 14 s",
        retryInS: 14,
        tier: "small",
        attempt: 1,
      }),
    );
    expect(s?.nodes[0]?.activity).toMatchObject({
      phase: "waiting",
      retryInS: 14,
      tier: "small",
    });
  });

  test("run_finished clears every activity", () => {
    const s = run(
      started,
      plan,
      nodeStarted(1),
      activity({ nodeId: "n1", phase: "writing", attempt: 1 }),
      {
        type: "run_finished",
        status: "failed",
        summary: "stopped",
        costUsd: 0,
        tokens: 0,
      },
    );
    expect(s?.activity).toBeNull();
    expect(s?.nodes[0]?.activity).toBeNull();
  });

  test("latestActivity picks the newest of run-wide and node activities", () => {
    const s = run(
      started,
      plan,
      nodeStarted(1),
      activity({ nodeId: "n1", phase: "writing", message: "Writing" }),
    );
    expect(s && latestActivity(s)?.message).toBe("Writing");
    expect(run(started)?.activity).toBeNull();
    const none = run(started);
    expect(none && latestActivity(none)).toBeNull();
  });
});

describe("per-attempt tier", () => {
  test("node_started.tier shows the tier this attempt uses", () => {
    expect(run(started, plan, nodeStarted(2, "strong"))?.nodes[0]?.tier).toBe(
      "strong",
    );
  });

  test("a null node_started.tier (older stored runs) keeps the planned tier", () => {
    const s = run(started, plan, { ...nodeStarted(1), tier: null } as Body);
    expect(s?.nodes[0]?.tier).toBe("small");
  });

  test("without node_started.tier the planned tier stays", () => {
    expect(run(started, plan, nodeStarted(1))?.nodes[0]?.tier).toBe("small");
  });
});
