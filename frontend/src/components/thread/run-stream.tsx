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
import { isNotFound } from "@/lib/api/errors";
import type { RunEvent } from "@/lib/api/types";
import {
  INITIAL_RUN_VIEW_STATE,
  type RunViewState,
  reduceRunEvents,
} from "@/lib/run-state";

/**
 * - `live`: subscribed and the run has not finished.
 * - `done`: `run_finished` arrived.
 * - `lost`: the stream ended or gave up reconnecting after events had
 *   already been applied (steps so far are kept).
 * - `error`: the stream failed before any event arrived (unreachable
 *   server, unknown run, ...).
 */
export type StreamConnection = "live" | "done" | "lost" | "error";

export interface RunStream {
  runId: string;
  state: RunViewState;
  connection: StreamConnection;
  /** With `connection === "error"`: the API reported the run as unknown (404). */
  notFound: boolean;
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
    outcome: "ended" | "failed";
    error: unknown;
  } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const settle = (outcome: "ended" | "failed", error: unknown = null) => {
      if (!controller.signal.aborted) {
        setSettled({ subscription, outcome, error });
      }
    };
    // Gate every callback on this subscription's signal: a stream that
    // ignores abort (or delivers late) must not touch state after cleanup.
    const onEvent = (e: RunEvent) => {
      if (!controller.signal.aborted) dispatch(e);
    };
    api.streamEvents(runId, onEvent, controller.signal).then(
      () => settle("ended"),
      (error: unknown) => settle("failed", error),
    );
    return () => controller.abort();
  }, [api, runId, subscription]);

  const retry = useCallback(() => setSubscription((n) => n + 1), []);

  const current = settled?.subscription === subscription ? settled : null;
  // Once any event has been applied the run exists and its steps are on
  // screen, so a stream that ends or gives up is a lost connection (the
  // HTTP client rejects when it runs out of reconnects). Only a failure
  // before the first event means the run could not be loaded at all.
  const connection: StreamConnection = state.finished
    ? "done"
    : !current
      ? "live"
      : current.outcome === "ended" || state.lastSeq > 0
        ? "lost"
        : "error";
  const notFound = connection === "error" && isNotFound(current?.error);

  return { runId, state, connection, notFound, retry };
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
