"use client";

import * as Tabs from "@radix-ui/react-tabs";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useApi } from "@/components/api/ApiProvider";
import { DemoBadge } from "@/components/shell/DemoBadge";
import { useRunStream } from "@/components/thread/run-stream";
import { errorDetail, isNotFound, retryWhileNotReady } from "@/lib/api/errors";
import type { RawInput, Transform } from "@/lib/api/types";
import type { RunViewState } from "@/lib/run-state";
import { CodeTab, type NoCodeReason } from "./CodeTab";
import { DataTab, DataTabSkeleton } from "./DataTab";
import { InputTab } from "./InputTab";
import { PlanTab } from "./PlanTab";
import { type PreviewPages, usePreviewPages } from "./use-preview-pages";
import { type Resource, useResource } from "./use-resource";

type Tab = "data" | "code" | "plan";
const TABS: ReadonlyArray<{ value: Tab; label: string }> = [
  { value: "data", label: "Data" },
  { value: "code", label: "Code" },
  { value: "plan", label: "Plan" },
];

/**
 * Transforms the live stream has reported. The API may already hold later
 * attempts (the demo mock returns them all up front), so while a run is in
 * flight only attempts that have finished or failed are shown.
 */
export function visibleTransforms(
  transforms: readonly Transform[],
  state: RunViewState | null,
): Transform[] {
  if (!state || state.finished) return [...transforms];
  const reported = new Map(state.nodes.map((n) => [n.id, n.attemptsReported]));
  return transforms.filter((t) => t.attempt <= (reported.get(t.nodeId) ?? 0));
}

/**
 * Changes only when an attempt produces a transform (fails or finishes), so
 * the Code tab refetches once per attempt rather than on every status change.
 */
const transformsKey = (state: RunViewState | null) =>
  state
    ? state.nodes
        // Nodes with nothing reported yet don't affect the key, so the plan
        // arriving (or a node starting) never triggers a refetch.
        .filter((n) => n.attemptsReported > 0)
        .map((n) => `${n.id}:${n.attemptsReported}`)
        .join(",")
    : "static";

function noCodeReason(state: RunViewState | null): NoCodeReason {
  if (!state) return "none";
  if (!state.finished) return "pending";
  if (state.noLlmWork) return "rules";
  return state.status === "failed" ? "failed" : "none";
}

function Unavailable({
  what,
  error,
  retry,
}: {
  what: string;
  error: unknown;
  retry: () => void;
}) {
  const detail = errorDetail(error);
  return (
    <div
      role="alert"
      className="grid min-h-40 place-items-center gap-3 rounded-xl bg-chip px-4 py-6 text-center"
    >
      <p className="text-sm">
        <span className="font-medium text-error">Couldn’t load {what}.</span>{" "}
        <span className="text-muted">
          {detail ?? "Check the connection and try again."}
        </span>
      </p>
      <button
        type="button"
        onClick={retry}
        className="glass glass-pill inline-flex h-10 items-center px-4 font-medium text-fg text-sm transition-colors duration-150 hover:bg-raised"
      >
        Retry
      </button>
    </div>
  );
}

function Loaded<T>({
  resource,
  what,
  fallback,
  children,
}: {
  resource: Resource<T>;
  what: string;
  fallback: ReactNode;
  children: (data: T) => ReactNode;
}) {
  switch (resource.status) {
    case "idle":
    case "loading":
      return (
        <output aria-label={`Loading ${what}`} className="contents">
          {fallback}
        </output>
      );
    case "error":
      return (
        <Unavailable
          what={what}
          error={resource.error}
          retry={resource.retry}
        />
      );
    case "ready":
      return children(resource.data);
  }
}

function Note({ children }: { children: ReactNode }) {
  return (
    <p className="grid min-h-40 place-items-center rounded-xl bg-chip px-4 text-center text-muted text-sm">
      {children}
    </p>
  );
}

/** The Data tab before `loaded` (or for a run that never loads). */
function InputPanel({
  state,
  pages,
  viewportHeight,
  sample,
}: {
  state: RunViewState;
  pages: PreviewPages<RawInput>;
  viewportHeight?: number;
  sample: boolean;
}) {
  switch (pages.status) {
    case "idle":
    case "loading":
      return (
        <output aria-label="Loading the uploaded file" className="contents">
          <DataTabSkeleton />
        </output>
      );
    case "error":
      // A run from before the raw-input route, or whose upload is gone.
      if (isNotFound(pages.error)) {
        return (
          <Note>
            {state.finished
              ? "No preview — this run didn’t load any data, and its uploaded file isn’t available."
              : "The uploaded file isn’t available yet. The before/after preview appears once the data has loaded."}
          </Note>
        );
      }
      return (
        <Unavailable
          what="the uploaded file"
          error={pages.error}
          retry={pages.retry}
        />
      );
    case "ready":
      return (
        <InputTab
          input={pages.preview}
          loadingMore={pages.loadingMore}
          moreError={pages.moreError}
          onLoadMore={pages.loadMore}
          neverLoaded={state.finished}
          sample={sample}
          viewportHeight={viewportHeight}
        />
      );
  }
}

/**
 * The Data tab body. The before/after preview exists only once the run
 * has loaded (the API answers 409 before), so until then — and for a run
 * that never loads — the raw uploaded rows are shown instead.
 */
function DataPanel({
  state,
  pages,
  input,
  inputWanted,
  viewportHeight,
  sample,
}: {
  state: RunViewState | null;
  pages: PreviewPages;
  input: PreviewPages<RawInput>;
  /** The view knows the run has not loaded (see `useInputWanted`). */
  inputWanted: boolean;
  viewportHeight?: number;
  /** The demo mock replays a fixture for this run (see `isSampleReplay`). */
  sample: boolean;
}) {
  if (state && !state.loaded) {
    // Not yet known whether the run loaded (its history is replaying):
    // a neutral placeholder, not the uploaded file's.
    if (!inputWanted) {
      return (
        <output aria-label="Loading the run’s data" className="contents">
          <DataTabSkeleton />
        </output>
      );
    }
    return (
      <InputPanel
        state={state}
        pages={input}
        viewportHeight={viewportHeight}
        sample={sample}
      />
    );
  }
  switch (pages.status) {
    case "idle":
    case "loading":
      return (
        <output aria-label="Loading the data preview" className="contents">
          <DataTabSkeleton />
        </output>
      );
    case "error":
      return (
        <Unavailable
          what="the data preview"
          error={pages.error}
          retry={pages.retry}
        />
      );
    case "ready":
      return (
        <DataTab
          preview={pages.preview}
          loadingMore={pages.loadingMore}
          moreError={pages.moreError}
          onLoadMore={pages.loadMore}
          viewportHeight={viewportHeight}
          sample={sample}
          rowsLoaded={state?.loaded?.rowsLoaded ?? null}
        />
      );
  }
}

/**
 * Quiet time after the latest event before a not-yet-loaded run's stream
 * counts as caught up. A reopened run's history replays as a burst far
 * faster than this; live events are spaced further apart.
 */
const REPLAY_SETTLE_MS = 250;

/**
 * Whether the raw upload is wanted. Only before `loaded` — and only once
 * the view knows the run has not loaded: a run page starts from the
 * initial state, so a finished, loaded run would otherwise ask for its
 * whole upload before the replay reaches `loaded`. A finished run without
 * `loaded` is known at once; an unfinished one once the stream has been
 * quiet for {@link REPLAY_SETTLE_MS} (the replay has caught up).
 */
function useInputWanted(runId: string, state: RunViewState | null): boolean {
  const notLoaded =
    state !== null && state.runId !== null && state.loaded === null;
  const finished = state?.finished ?? false;
  // The run this view saw a quiet stream for; it never un-settles.
  const [settledFor, setSettledFor] = useState<string | null>(null);
  const settled = settledFor === runId;
  // Changes with every event while waiting, restarting the quiet timer.
  const waitKey =
    notLoaded && !finished && !settled ? `${runId}:${state.lastSeq}` : null;
  useEffect(() => {
    if (waitKey === null) return;
    const timer = setTimeout(() => setSettledFor(runId), REPLAY_SETTLE_MS);
    return () => clearTimeout(timer);
  }, [waitKey, runId]);
  return notLoaded && (finished || settled);
}

const CODE_SKELETON = (
  <div aria-hidden="true" className="space-y-2.5">
    <span className="block h-4 w-3/4 animate-pulse rounded-full bg-chip" />
    <span className="block h-8 w-48 animate-pulse rounded-[12px] bg-chip" />
    <span className="block h-48 animate-pulse rounded-xl bg-chip" />
  </div>
);

export interface SidePanelProps {
  runId: string;
  /** The run's live view state; `null` outside a run stream. */
  state: RunViewState | null;
  /** Fixed Data-table viewport height in px; tests only (jsdom has no layout). */
  dataViewportHeight?: number;
  /** The run's stream is connected and unfinished (live activity lines). */
  live?: boolean;
}

/**
 * Run details: Data (before/after preview), Code (generated transforms and
 * repair diffs) and Plan (step DAG) tabs. Each tab's data is fetched the
 * first time the tab is opened and kept while switching tabs; the preview
 * loads once the run has loaded (page by page, as the table scrolls),
 * transforms once per attempt that fails or finishes.
 */
export function SidePanel({
  runId,
  state,
  dataViewportHeight,
  live = false,
}: SidePanelProps) {
  const api = useApi();
  const sample = api.isSampleReplay?.(runId) ?? false;
  const [tab, setTab] = useState<Tab>("data");
  const [opened, setOpened] = useState<ReadonlySet<Tab>>(
    () => new Set(["data"]),
  );

  // Outside a run stream (state null) the run is taken as complete.
  const loaded = state === null || state.loaded !== null;
  const pages = usePreviewPages(
    opened.has("data") && loaded ? runId : null,
    // Just after `loaded` the preview may still be being written: the API
    // answers 409 `not_ready` and the contract says to retry shortly.
    // The retries stop when the panel closes or the run changes.
    (offset, limit, signal) =>
      retryWhileNotReady(
        () => api.getPreview(runId, { offset, limit }),
        undefined,
        signal,
      ),
  );
  // The raw upload: available from creation, wanted only until `loaded`.
  // The request aborts when `loaded` arrives, the run changes or the panel
  // closes (the server reads the whole upload to answer it).
  const inputWanted = useInputWanted(runId, state);
  const input = usePreviewPages(
    opened.has("data") && inputWanted ? `${runId}|input` : null,
    (offset, limit, signal) => api.getInput(runId, { offset, limit }, signal),
  );
  const transforms = useResource(
    opened.has("code") ? `${runId}|${transformsKey(state)}` : null,
    () => api.getTransforms(runId),
  );

  const nodes = state?.nodes;
  const planNodes = useMemo(
    () => (nodes ?? []).map((n) => ({ id: n.id, title: n.title })),
    [nodes],
  );

  const onTab = (value: string) => {
    const next = TABS.find((t) => t.value === value)?.value;
    if (!next) return;
    setTab(next);
    setOpened((prev) => (prev.has(next) ? prev : new Set(prev).add(next)));
  };

  return (
    <Tabs.Root
      value={tab}
      onValueChange={onTab}
      className="flex h-full min-h-0 flex-col gap-4"
    >
      <div
        data-testid="side-panel-tabs"
        className="flex shrink-0 items-center gap-2"
      >
        <Tabs.List
          aria-label="Run details"
          className="grid min-w-0 flex-1 grid-cols-3 gap-1 rounded-[12px] bg-chip p-1"
        >
          {TABS.map((t) => (
            <Tabs.Trigger
              key={t.value}
              value={t.value}
              className="h-9 rounded-[9px] font-medium text-muted text-sm transition-colors duration-150 hover:text-fg data-[state=active]:bg-raised data-[state=active]:text-fg data-[state=active]:shadow-[0_1px_4px_rgb(0_0_0/0.06),inset_0_1px_0_rgb(255_255_255/0.5)]"
            >
              {t.label}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        <DemoBadge variant="compact" />
      </div>

      <Tabs.Content
        value="data"
        className="flex min-h-0 flex-1 flex-col focus-visible:outline-offset-4"
      >
        <DataPanel
          state={state}
          pages={pages}
          input={input}
          inputWanted={inputWanted}
          viewportHeight={dataViewportHeight}
          sample={sample}
        />
      </Tabs.Content>

      <Tabs.Content value="code" className="focus-visible:outline-offset-4">
        <Loaded
          resource={transforms}
          what="the generated code"
          fallback={CODE_SKELETON}
        >
          {(data) => (
            <CodeTab
              transforms={visibleTransforms(data, state)}
              nodes={planNodes}
              emptyReason={noCodeReason(state)}
            />
          )}
        </Loaded>
      </Tabs.Content>

      <Tabs.Content value="plan" className="focus-visible:outline-offset-4">
        <PlanTab state={state} live={live} />
      </Tabs.Content>
    </Tabs.Root>
  );
}

/**
 * The run page's side panel: reads the same live {@link RunViewState} as
 * the thread from the page's `RunStreamProvider`. Keyed by run so switching
 * runs never shows the previous run's data.
 */
export function RunSidePanel({ runId }: { runId: string }) {
  const stream = useRunStream();
  return (
    <SidePanel
      key={runId}
      runId={runId}
      state={stream?.state ?? null}
      live={stream?.connection === "live"}
    />
  );
}
