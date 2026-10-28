import type { RunEvent } from "../types";

type DistributiveOmit<T, K extends PropertyKey> = T extends unknown
  ? Omit<T, K>
  : never;

/** A run event without its envelope; `seq` and `at` are assigned in order. */
export type EventBody = DistributiveOmit<RunEvent, "seq" | "at">;

/**
 * Builds a run's event log: `seq` counts up from 1 and `at` is `startIso`
 * plus each step's cumulative offset, so fixtures stay ordered by
 * construction.
 */
export function buildEvents(
  startIso: string,
  steps: readonly (readonly [afterMs: number, body: EventBody])[],
): RunEvent[] {
  let t = Date.parse(startIso);
  return steps.map(([afterMs, body], i) => {
    t += afterMs;
    return { ...body, seq: i + 1, at: new Date(t).toISOString() };
  });
}
