"use client";

import { useCallback, useEffect, useState } from "react";
import { useApi } from "@/components/api/ApiProvider";
import type { ApiClient, RunSummary } from "@/lib/api/types";

export type RunsState =
  | { status: "loading" }
  /** `fetchedAt` (epoch ms) is the "now" the list is grouped against. */
  | { status: "ready"; runs: RunSummary[]; fetchedAt: number }
  | { status: "error" };

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
function failed(prev: RunsState): RunsState {
  return prev.status === "ready" ? prev : { status: "error" };
}

/**
 * The run list for the sidebar: stale-while-revalidate from the last list
 * this client returned, refetched on every mount. With nothing to show, a
 * failure becomes `error` and `retry` refetches.
 */
export function useRuns(): { state: RunsState; retry: () => void } {
  const api = useApi();
  const [state, setState] = useState<RunsState>(
    () => lastRuns.get(api) ?? { status: "loading" },
  );

  useEffect(() => {
    let live = true;
    fetchRuns(api).then(
      (ready) => {
        if (live) setState(ready);
      },
      () => {
        if (live) setState(failed);
      },
    );
    return () => {
      live = false;
    };
  }, [api]);

  const retry = useCallback(() => {
    setState({ status: "loading" });
    fetchRuns(api).then(setState, () => setState(failed));
  }, [api]);

  return { state, retry };
}
