import type { ActivityView } from "@/lib/run-state";

/**
 * Whole seconds from `at` (an ISO timestamp from the server) to `nowMs`.
 * Never negative: a server clock slightly ahead of ours reads as 0.
 */
export function elapsedSeconds(at: string, nowMs: number): number {
  const start = Date.parse(at);
  if (!Number.isFinite(start)) return 0;
  return Math.max(0, Math.floor((nowMs - start) / 1_000));
}

/** "12 s", or from a minute up "1 m 05 s". */
export function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  const m = Math.floor(seconds / 60);
  return `${m} m ${String(seconds % 60).padStart(2, "0")} s`;
}

/** A server message's own trailing "— retrying in 14 s" (the UI counts it). */
const TRAILING_COUNTDOWN = /\s*[—–-]\s*retrying in \d+\s*s\.?$/i;

export function stripRetryCountdown(message: string): string {
  return message.replace(TRAILING_COUNTDOWN, "");
}

export interface ActivityClock {
  /** The activity's copy, minus any static countdown. */
  message: string;
  /** "12 s" elapsed, or for a back-off "retrying in 9 s" / "retrying now". */
  clock: string;
  /** A rate-limit back-off (`phase: "waiting"`). */
  waiting: boolean;
}

/** What the live activity line reads at `nowMs`. */
export function activityClock(
  activity: ActivityView,
  nowMs: number,
): ActivityClock {
  const elapsed = elapsedSeconds(activity.at, nowMs);
  const waiting = activity.phase === "waiting";
  if (waiting && activity.retryInS != null) {
    const left = Math.max(0, Math.ceil(activity.retryInS) - elapsed);
    return {
      message: stripRetryCountdown(activity.message),
      clock: left > 0 ? `retrying in ${left} s` : "retrying now",
      waiting,
    };
  }
  return { message: activity.message, clock: formatElapsed(elapsed), waiting };
}
