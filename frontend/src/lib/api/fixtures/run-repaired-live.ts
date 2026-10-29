import type { RunEvent } from "../types";
import type { EventBody } from "./build";
import { RUN_REPAIRED, RUN_REPAIRED_EVENTS } from "./run-repaired";

type ActivityBody = Extract<EventBody, { type: "activity" }>;

const rows = (RUN_REPAIRED.rows ?? 0).toLocaleString("en-IN");

const act = (
  over: Partial<ActivityBody> & Pick<ActivityBody, "phase" | "message">,
): ActivityBody => ({
  type: "activity",
  nodeId: null,
  tier: null,
  attempt: null,
  retryInS: null,
  ...over,
});

/**
 * The `activity` lines a live run of run-repaired would stream, keyed by
 * the fixture event they follow (`type`, plus `nodeId:attempt` for node
 * events). Copy follows the contract's examples.
 */
const AFTER: Record<string, readonly ActivityBody[]> = {
  rules_applied: [
    act({
      phase: "planning",
      message: "Asking the small model to plan the repairs",
    }),
  ],
  "node_started n1:1": [
    act({
      nodeId: "n1",
      phase: "writing",
      message: "Asking the small model to write a fix for amt",
      tier: "small",
      attempt: 1,
    }),
    act({
      nodeId: "n1",
      phase: "sandbox",
      message: `Running the fix in the sandbox on ${rows} rows`,
      tier: "small",
      attempt: 1,
    }),
  ],
  "attempt_failed n1:1": [
    act({
      nodeId: "n1",
      phase: "waiting",
      message: "Rate limit reached on the strong model — retrying in 3 s",
      tier: "strong",
      attempt: 2,
      retryInS: 3,
    }),
  ],
  "node_started n1:2": [
    act({
      nodeId: "n1",
      phase: "writing",
      message: "Asking the strong model to write a fix for amt",
      tier: "strong",
      attempt: 2,
    }),
    act({
      nodeId: "n1",
      phase: "verifying",
      message: "The verifier is checking the result",
      tier: "strong",
      attempt: 2,
    }),
  ],
  "node_started n2:1": [
    act({
      nodeId: "n2",
      phase: "writing",
      message: "Asking the small model to flag implausible o_totalprice values",
      tier: "small",
      attempt: 1,
    }),
  ],
  verified: [
    act({
      phase: "loading",
      message: `Loading ${rows} rows into the warehouse`,
    }),
  ],
};

function keyOf(e: RunEvent): string {
  if (e.type === "node_started" || e.type === "attempt_failed") {
    return `${e.type} ${e.nodeId}:${e.attempt}`;
  }
  return e.type;
}

/**
 * run-repaired's log as a live run streams it: the fixture's events with
 * `activity` lines in between, `seq` renumbered to stay gapless. Each
 * activity shares the `at` of the event it follows (the mock re-stamps a
 * created run's events as it streams them anyway).
 */
export const RUN_REPAIRED_LIVE_EVENTS: readonly RunEvent[] =
  RUN_REPAIRED_EVENTS.flatMap((e) => [
    e,
    ...(AFTER[keyOf(e)] ?? []).map((body) => ({ ...body, seq: 0, at: e.at })),
  ]).map((e, i) => ({ ...e, seq: i + 1 }));
