"use client";

import { useEffect, useState } from "react";
import { activityClock } from "@/lib/activity";
import type { ActivityView } from "@/lib/run-state";

/**
 * The current time, re-read once a second on the second boundaries of
 * `originMs`, so "12 s" turns over as the phase's own seconds do.
 */
function useSecondClock(originMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const schedule = () => {
      const offset = (((Date.now() - originMs) % 1_000) + 1_000) % 1_000;
      timer = setTimeout(() => {
        setNow(Date.now());
        schedule();
      }, 1_000 - offset);
    };
    schedule();
    return () => clearTimeout(timer);
  }, [originMs]);
  return now;
}

/**
 * The live "what is happening now" line under an in-flight step: a small
 * spinner, the server's message, and a clock that ticks every second
 * (elapsed time, or a back-off's "retrying in N s" countdown).
 *
 * Deliberately not a live region: the thread's single polite region
 * announces each new phase once, so the per-second tick stays silent.
 * Under reduced motion the spinner holds still; the clock still ticks.
 */
export function ActivityLine({ activity }: { activity: ActivityView }) {
  const origin = Date.parse(activity.at);
  const now = useSecondClock(Number.isFinite(origin) ? origin : 0);
  const { message, clock, waiting } = activityClock(activity, now);
  return (
    <p
      data-testid="activity-line"
      data-phase={activity.phase}
      className="rise-in mt-1.5 flex min-w-0 items-start gap-2 text-xs leading-5"
    >
      <span className="sr-only">Now: </span>
      <span className="grid h-5 shrink-0 place-items-center">
        <span
          data-testid="activity-spinner"
          aria-hidden="true"
          className={`block size-3 animate-spin rounded-full border-[1.5px] motion-reduce:animate-none ${
            waiting
              ? "border-warn/25 border-t-warn"
              : "border-accent/25 border-t-accent"
          }`}
        />
      </span>
      <span className="min-w-0 text-pretty text-fg/85">
        {message}
        <span
          className={`whitespace-nowrap tabular-nums ${waiting ? "font-medium text-warn" : "text-muted"}`}
        >
          {waiting ? " — " : " · "}
          {clock}
        </span>
      </span>
    </p>
  );
}
