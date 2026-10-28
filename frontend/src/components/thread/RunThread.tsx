"use client";

import { useMemo } from "react";
import type { RunStatus } from "@/lib/api/types";
import { formatCount, formatMs, formatUsd } from "@/lib/format";
import type { RunViewState } from "@/lib/run-state";
import { FileChip } from "./FileChip";
import { type RunStream, RunStreamProvider, useRunStream } from "./run-stream";
import { StepList } from "./StepList";
import { announceLatest, buildSteps } from "./steps";

const OUTCOME: Partial<Record<RunStatus, { label: string; tone: string }>> = {
  succeeded: { label: "Succeeded", tone: "text-ok" },
  quarantined: { label: "Quarantined", tone: "text-warn" },
  failed: { label: "Failed", tone: "text-error" },
};

const BUBBLE =
  "ml-auto w-fit max-w-[88%] rounded-[18px] bg-raised px-4 py-3 shadow-[inset_0_1px_0_rgb(255_255_255/0.5)] sm:max-w-[78%]";

function UserBubble({ state }: { state: RunViewState }) {
  if (!state.fileName) {
    // Before `run_started`: hold the bubble's place without fake text.
    return (
      <div aria-hidden="true" className={`${BUBBLE} space-y-2`}>
        <span className="block h-6 w-52 animate-pulse rounded-[10px] bg-chip" />
        <span className="block h-4 w-40 animate-pulse rounded-full bg-chip" />
      </div>
    );
  }
  return (
    <div className={`${BUBBLE} rise-in`}>
      <p className="sr-only">You:</p>
      <FileChip name={state.fileName} rows={state.rows} className="mb-1.5" />
      <p className="text-[0.9375rem] leading-6">
        Load this into the {state.table} table.
      </p>
    </div>
  );
}

function duration(state: RunViewState): number | null {
  if (!state.startedAt || !state.finishedAt) return null;
  const ms = Date.parse(state.finishedAt) - Date.parse(state.startedAt);
  return Number.isFinite(ms) && ms >= 0 ? ms : null;
}

/** The assistant's plain-language wrap-up once `run_finished` arrives. */
function RunSummary({ state }: { state: RunViewState }) {
  const outcome = OUTCOME[state.status];
  const ms = duration(state);
  const meta = [
    `${formatCount(state.totals.tokens)} tokens`,
    ms == null ? null : formatMs(ms),
  ].filter(Boolean);
  return (
    <section aria-label="Run summary" className="rise-in space-y-2">
      <div className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className="accent-fill grid size-6 place-items-center rounded-full"
        >
          <svg
            viewBox="0 0 24 24"
            width="14"
            height="14"
            fill="none"
            aria-hidden="true"
          >
            <path
              d="M6 6v6M12 6v6M18 6v6M4 6h16M4 17c2.7-1.9 5.3-1.9 8 0s5.3 1.9 8 0"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
            />
          </svg>
        </span>
        <span className="font-medium text-sm">Sluice</span>
        {outcome && (
          <span
            className={`rounded-full bg-chip px-2 py-0.5 font-medium text-xs ${outcome.tone}`}
          >
            {outcome.label}
          </span>
        )}
      </div>
      {state.summary && (
        <p className="text-pretty text-[0.9375rem] leading-relaxed">
          {state.summary}
        </p>
      )}
      <p className="text-muted text-xs">
        <span className="font-medium text-fg">
          Total cost {formatUsd(state.totals.costUsd)}
        </span>
        {meta.map((m) => (
          <span key={m}> · {m}</span>
        ))}
      </p>
    </section>
  );
}

function StreamProblem({
  connection,
  onRetry,
}: {
  connection: "lost" | "error";
  onRetry: () => void;
}) {
  const copy =
    connection === "lost"
      ? {
          title: "Connection lost",
          body: "The live updates stopped before the run finished. Steps so far are kept.",
        }
      : {
          title: "Couldn't load this run",
          body: "It may not exist, or the server is unreachable.",
        };
  return (
    <div
      role="alert"
      className="rise-in flex flex-wrap items-center gap-x-4 gap-y-3 rounded-2xl bg-chip px-4 py-3"
    >
      <div className="min-w-0 flex-1">
        <p className="font-medium text-sm">{copy.title}</p>
        <p className="text-muted text-sm">{copy.body}</p>
      </div>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex min-h-10 items-center rounded-full bg-raised px-4 font-medium text-sm shadow-[0_1px_4px_rgb(0_0_0/0.06)] transition-colors duration-150 hover:bg-glass-solid"
      >
        Retry
      </button>
    </div>
  );
}

function Thread({ stream }: { stream: RunStream }) {
  const { state, connection, retry } = stream;
  const live = connection === "live";
  const steps = useMemo(() => buildSteps(state, live), [state, live]);
  const announcement = announceLatest(state, steps);

  return (
    <article aria-labelledby="run-title" className="flex flex-col gap-5">
      <h1 id="run-title" className="sr-only">
        Run {state.fileName ?? stream.runId}
      </h1>
      <UserBubble state={state} />
      {steps.length > 0 && <StepList state={state} steps={steps} />}
      {state.finished && <RunSummary state={state} />}
      {(connection === "lost" || connection === "error") && (
        <StreamProblem connection={connection} onRetry={retry} />
      )}
      {/* One polite region with only the newest change, never the list. */}
      <output aria-live="polite" aria-atomic="true" className="sr-only">
        {live && !state.runId ? "Connecting to the run…" : announcement}
      </output>
    </article>
  );
}

export interface RunThreadProps {
  runId: string;
}

/**
 * The chat-style run thread: the user's request (file + table), the live
 * step list, and the summary once the run finishes. Uses the surrounding
 * {@link RunStreamProvider} when it serves this run (so a details panel can
 * share the same state), otherwise opens its own subscription.
 */
export function RunThread({ runId }: RunThreadProps) {
  const stream = useRunStream();
  if (stream?.runId !== runId) {
    return (
      <RunStreamProvider runId={runId}>
        <RunThread runId={runId} />
      </RunStreamProvider>
    );
  }
  return <Thread stream={stream} />;
}
