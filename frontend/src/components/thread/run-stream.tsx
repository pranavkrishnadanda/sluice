"use client";

import {
  createContext,
  type ReactNode,
  use,
  useCallback,
  useEffect,
  useReducer,
  useState,
} from "react";
import { useApi } from "@/components/api/ApiProvider";
import type { RunEvent } from "@/lib/api/types";
import {
  INITIAL_RUN_VIEW_STATE,
  type RunViewState,
  reduceRunEvents,
} from "@/lib/run-state";

/**
 * - `live`: subscribed and the run has not finished.
 * - `done`: `run_finished` arrived.
 * - `lost`: the stream ended without `run_finished` (connection dropped).
 * - `error`: the stream could not be opened (e.g. unknown run).
 */
export type StreamConnection = "live" | "done" | "lost" | "error";

export interface RunStream {
  runId: string;
  state: RunViewState;
  connection: StreamConnection;
  /** Re-subscribes; replayed events are dropped by the reducer's seq check. */
  retry: () => void;
}

const reducer = (state: RunViewState, e: RunEvent) => reduceRunEvents(state, e);

/**
 * Subscribes to a run's event stream and folds it into {@link RunViewState}.
 * The subscription is aborted on unmount and before every re-subscribe.
 * State is not reset on retry: the reducer de-duplicates by `seq`.
 */
function useRunStreamSubscription(runId: string): RunStream {
  const api = useApi();
  const [state, dispatch] = useReducer(reducer, INITIAL_RUN_VIEW_STATE);
  const [subscription, setSubscription] = useState(0);
  // Tagged with the subscription it belongs to, so a retry clears it
  // without a reset inside the effect.
  const [settled, setSettled] = useState<{
    subscription: number;
    outcome: "ended" | "error";
  } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const settle = (outcome: "ended" | "error") => {
      if (!controller.signal.aborted) setSettled({ subscription, outcome });
    };
    api.streamEvents(runId, dispatch, controller.signal).then(
      () => settle("ended"),
      () => settle("error"),
    );
    return () => controller.abort();
  }, [api, runId, subscription]);

  const retry = useCallback(() => setSubscription((n) => n + 1), []);

  const outcome =
    settled?.subscription === subscription ? settled.outcome : null;
  const connection: StreamConnection = state.finished
    ? "done"
    : outcome === "ended"
      ? "lost"
      : (outcome ?? "live");

  return { runId, state, connection, retry };
}

const RunStreamContext = createContext<RunStream | null>(null);

function RunStreamScope({
  runId,
  children,
}: {
  runId: string;
  children: ReactNode;
}) {
  const stream = useRunStreamSubscription(runId);
  return <RunStreamContext value={stream}>{children}</RunStreamContext>;
}

/**
 * Owns one run's live stream for everything beneath it, so the thread and
 * the details panel render the same {@link RunViewState} from a single
 * subscription. Keyed by `runId`: switching runs starts from a fresh state
 * (the reducer would otherwise ignore the new run's low `seq` numbers).
 */
export function RunStreamProvider({
  runId,
  children,
}: {
  runId: string;
  children: ReactNode;
}) {
  return (
    <RunStreamScope key={runId} runId={runId}>
      {children}
    </RunStreamScope>
  );
}

/** The nearest {@link RunStreamProvider}'s stream, or `null` outside one. */
export function useRunStream(): RunStream | null {
  return use(RunStreamContext);
}
