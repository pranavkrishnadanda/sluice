import { describe, expect, test } from "vitest";
import { announceLatest, buildSteps } from "@/components/thread/steps";
import {
  activityClock,
  elapsedSeconds,
  formatElapsed,
  stripRetryCountdown,
} from "@/lib/activity";
import type { RunEvent } from "@/lib/api/types";
import {
  type ActivityView,
  type RunViewState,
  reduceRunEvents,
} from "@/lib/run-state";

const T0 = Date.parse("2026-10-28T09:00:00.000Z");
const AT = new Date(T0).toISOString();

const view = (over: Partial<ActivityView> = {}): ActivityView => ({
  seq: 1,
  at: AT,
  phase: "writing",
  message: "Asking the strong model to write a fix for o_orderdate",
  tier: "strong",
  attempt: 1,
  retryInS: null,
  ...over,
});

describe("elapsed time", () => {
  test("whole seconds since the phase started, never negative", () => {
    expect(elapsedSeconds(AT, T0)).toBe(0);
    expect(elapsedSeconds(AT, T0 + 61_900)).toBe(61);
    // Server clock ahead of ours: show 0, not a negative time.
    expect(elapsedSeconds(AT, T0 - 5_000)).toBe(0);
    expect(elapsedSeconds("not a date", T0)).toBe(0);
  });

  test("formats seconds, then minutes and seconds", () => {
    expect(formatElapsed(0)).toBe("0 s");
    expect(formatElapsed(59)).toBe("59 s");
    expect(formatElapsed(61)).toBe("1 m 01 s");
    expect(formatElapsed(3_725)).toBe("62 m 05 s");
  });
});

describe("activityClock", () => {
  test("ticks elapsed for a working phase", () => {
    expect(activityClock(view(), T0 + 12_000)).toEqual({
      message: "Asking the strong model to write a fix for o_orderdate",
      clock: "12 s",
      waiting: false,
    });
  });

  test("counts a waiting phase down and replaces the server's static number", () => {
    const waiting = view({
      phase: "waiting",
      message: "Rate limit reached on the small model — retrying in 14 s",
      retryInS: 14,
    });
    expect(activityClock(waiting, T0)).toEqual({
      message: "Rate limit reached on the small model",
      clock: "retrying in 14 s",
      waiting: true,
    });
    expect(activityClock(waiting, T0 + 9_000).clock).toBe("retrying in 5 s");
    expect(activityClock(waiting, T0 + 30_000).clock).toBe("retrying now");
  });

  test("a waiting phase without retryInS ticks elapsed", () => {
    const waiting = view({ phase: "waiting", message: "Waiting" });
    expect(activityClock(waiting, T0 + 3_000)).toMatchObject({
      clock: "3 s",
      waiting: true,
    });
  });

  test("strips only a trailing countdown", () => {
    expect(stripRetryCountdown("Busy — retrying in 3 s")).toBe("Busy");
    expect(stripRetryCountdown("Busy - retrying in 3s.")).toBe("Busy");
    expect(stripRetryCountdown("Retrying in 3 s soon")).toBe(
      "Retrying in 3 s soon",
    );
  });
});

type Body = RunEvent extends infer E
  ? E extends RunEvent
    ? Omit<E, "seq" | "at">
    : never
  : never;
const reduce = (...bodies: Body[]): RunViewState =>
  bodies
    .map((b, i) => ({ ...b, seq: i + 1, at: AT }) as RunEvent)
    .reduce<RunViewState | undefined>(
      reduceRunEvents,
      undefined,
    ) as RunViewState;

const started: Body = {
  type: "run_started",
  runId: "r",
  fileName: "f.csv",
  table: "orders",
  rows: 10,
};
const activity = (
  over: Partial<Extract<Body, { type: "activity" }>>,
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
const plan: Body = {
  type: "plan_created",
  nodes: [
    {
      id: "n1",
      title: "Fix dates",
      columns: ["d"],
      tier: "small",
      dependsOn: [],
    },
  ],
};

describe("steps carry the live activity", () => {
  test("a run-wide activity rides on the in-flight milestone", () => {
    const state = reduce(
      started,
      { type: "profiled", issues: [] },
      { type: "rules_applied", actions: [] },
      activity({}),
    );
    const steps = buildSteps(state, true);
    const active = steps.find((s) => s.status === "active");
    expect(active?.id).toBe("plan");
    expect(active?.activity?.message).toBe("Planning the repairs");
  });

  test("a node activity rides on its node and replaces the generic meta", () => {
    const state = reduce(
      started,
      { type: "profiled", issues: [] },
      { type: "rules_applied", actions: [] },
      plan,
      { type: "node_started", nodeId: "n1", attempt: 1, maxAttempts: 3 },
      activity({
        nodeId: "n1",
        phase: "sandbox",
        message: "Running the fix",
        attempt: 1,
      }),
    );
    const step = buildSteps(state, true).find((s) => s.id === "node-n1");
    expect(step?.activity?.phase).toBe("sandbox");
    expect(step?.meta).toBe("attempt 1/3");
  });

  test("a phase after the last milestone gets its own in-flight row", () => {
    const state = reduce(
      started,
      { type: "profiled", issues: [] },
      { type: "rules_applied", actions: [] },
      { type: "plan_created", nodes: [] },
      { type: "verified", checksPassed: 3, checksTotal: 3 },
      { type: "loaded", rowsLoaded: 10, rowsQuarantined: 0, snapshotId: "s" },
      activity({ phase: "previewing", message: "Building the preview" }),
    );
    const last = buildSteps(state, true).at(-1);
    expect(last).toMatchObject({ id: "finishing", status: "active" });
    expect(last?.activity?.phase).toBe("previewing");
  });

  test("no activity is shown once the stream is not live", () => {
    const state = reduce(started, activity({}));
    expect(buildSteps(state, false).some((s) => s.activity)).toBe(false);
  });

  test("the announcement names the phase, without the ticking clock", () => {
    const state = reduce(
      started,
      { type: "profiled", issues: [] },
      activity({ phase: "mapping", message: "Mapping the file's columns" }),
    );
    const text = announceLatest(state, buildSteps(state, true));
    expect(text).toMatch(/Profiled/);
    expect(text).toMatch(/Now: Mapping the file's columns\.$/);
  });
});
