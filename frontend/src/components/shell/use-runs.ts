"use client";

import { useCallback, useEffect, useState } from "react";
import { useApi } from "@/components/api/ApiProvider";
import { useRunStream } from "@/components/thread/run-stream";
import type { ApiClient, RunSummary } from "@/lib/api/types";

export type RunsState =
  | { status: "loading" }
  /** `fetchedAt` (epoch ms) is the "now" the list is grouped against. */
  | { status: "ready"; runs: RunSummary[]; fetchedAt: number }
  | { status: "error"; error: unknown };

type Ready = Extract<RunsState, { status: "ready" }>;

/*
 * Last list seen per client. Each page renders its own AppShell, so the
 * sidebar remounts on navigation; starting from the cached list (and
 * refetching in the background) avoids flashing the skeleton every time.
 */
const lastRuns = new WeakMap<ApiClient, Ready>();

function fetchRuns(api: ApiClient): Promise<Ready> {
  return api.listRuns().then((runs) => {
    const ready: Ready = { status: "ready", runs, fetchedAt: Date.now() };
    lastRuns.set(api, ready);
    return ready;
  });
}

/** A failed refetch keeps a list we already have. */
const failed =
  (error: unknown) =>
  (prev: RunsState): RunsState =>
    prev.status === "ready" ? prev : { status: "error", error };

/**
 * The run list for the sidebar: stale-while-revalidate from the last list
 * this client returned, refetched on every mount and again when the open
 * run's `run_finished` arrives (so its status dot settles). With nothing to
 * show, a failure becomes `error` and `retry` refetches.
 */
export function useRuns(): { state: RunsState; retry: () => void } {
  const api = useApi();
  const stream = useRunStream();
  // Set once the open run's stream reports `run_finished`.
  const finishedRun = stream?.state.finished ? stream.runId : null;
  const [state, setState] = useState<RunsState>(
    () => lastRuns.get(api) ?? { status: "loading" },
  );

  useEffect(() => {
    let live = true;
    fetchRuns(api).then(
      (ready) => {
        if (live) setState(ready);
      },
      (error: unknown) => {
        if (live) setState(failed(error));
      },
    );
    return () => {
      live = false;
    };
  }, [api]);

  // The open run just finished: refetch so its status dot settles. A failed
  // refetch keeps the list on screen.
  useEffect(() => {
    if (finishedRun === null) return;
    let live = true;
    fetchRuns(api).then(
      (ready) => {
        if (live) setState(ready);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [api, finishedRun]);

  const retry = useCallback(() => {
    setState({ status: "loading" });
    fetchRuns(api).then(setState, (error: unknown) => setState(failed(error)));
  }, [api]);

  return { state, retry };
}
