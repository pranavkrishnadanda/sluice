"use client";

import { useId, useMemo } from "react";
import { ActivityLine } from "@/components/thread/ActivityLine";
import { formatMs, formatUsd } from "@/lib/format";
import {
  type NodeViewState,
  type RunViewState,
  STOPPED_NODE_NOTE,
} from "@/lib/run-state";
import { planWaves } from "./plan-layout";

type CardStatus =
  | "pending"
  | "running"
  | "retrying"
  | "done"
  | "failed"
  | "stopped";

/** A failed node the run's end cut off mid-attempt reads as stopped. */
function cardStatus(n: NodeViewState): CardStatus {
  if (n.status === "failed" && n.lastError === STOPPED_NODE_NOTE) {
    return "stopped";
  }
  return n.status;
}

const STATUS_TONE: Record<CardStatus, string> = {
  pending: "text-muted",
  running: "text-accent",
  retrying: "text-warn",
  done: "text-ok",
  failed: "text-error",
  stopped: "text-error",
};

const CHIP =
  "inline-flex h-5 items-center rounded-full bg-chip px-2 font-medium text-[0.6875rem] leading-none";

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "2 attempts · retried on strong · 7.3 s · $0.0018 · depends on: 1, 2". */
function nodeMeta(n: NodeViewState, deps: number[]): string {
  const parts: string[] = [];
  if (n.status === "running" || n.status === "retrying") {
    const of = n.maxAttempts ? `/${n.maxAttempts}` : "";
    parts.push(`attempt ${n.attempt}${of}`);
  } else if (n.attempt > 0) {
    parts.push(plural(n.attempt, "attempt", "attempts"));
  }
  if (n.attempt > 1) parts.push(`retried on ${n.tier}`);
  if (n.status === "done") {
    if (n.latencyMs != null) parts.push(formatMs(n.latencyMs));
    parts.push(formatUsd(n.costUsd));
  }
  if (deps.length > 0) parts.push(`depends on: ${deps.join(", ")}`);
  return parts.join(" · ");
}

function NodeCard({
  node,
  step,
  deps,
  live,
}: {
  node: NodeViewState;
  step: number;
  deps: number[];
  live: boolean;
}) {
  const status = cardStatus(node);
  const meta = nodeMeta(node, deps);
  const open = node.status === "running" || node.status === "retrying";
  return (
    <li
      data-testid={`plan-node-${node.id}`}
      data-node-id={node.id}
      data-status={status}
      className="plan-card min-w-0 rounded-2xl border-[1.5px] px-3.5 py-3"
    >
      <div className="flex min-w-0 items-start gap-2.5">
        <span className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-full bg-chip font-medium text-fg text-xs tabular-nums">
          <span className="sr-only">Step </span>
          {step}
        </span>
        <div className="min-w-0 flex-1 space-y-0.5">
          {node.columns.length > 0 && (
            <p className="break-all font-mono text-[0.6875rem] text-muted leading-5">
              {node.columns.join(", ")}
            </p>
          )}
          <p className="text-pretty break-words font-medium text-sm leading-5">
            {node.title}
          </p>
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5 pl-8.5">
        <span className={`${CHIP} font-mono text-fg`}>{node.tier}</span>
        <span className={`${CHIP} ${STATUS_TONE[status]}`}>{status}</span>
      </div>
      {meta && (
        <p className="mt-1.5 break-words pl-8.5 text-muted text-xs leading-5">
          {meta}
        </p>
      )}
      {live && open && node.activity && (
        <div className="pl-8.5">
          <ActivityLine key={node.activity.seq} activity={node.activity} />
        </div>
      )}
    </li>
  );
}

/** The downward link between one wave and the next. */
function Connector() {
  return (
    <div aria-hidden="true" className="flex justify-center py-1.5">
      <svg
        aria-hidden="true"
        focusable="false"
        viewBox="0 0 12 22"
        width="12"
        height="22"
        fill="none"
        className="text-muted"
      >
        <path
          d="M6 1v18M2 15l4 4 4-4"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  );
}

function Wave({
  nodes,
  stepOf,
  live,
}: {
  nodes: NodeViewState[];
  stepOf: Map<string, number>;
  live: boolean;
}) {
  const headingId = useId();
  const label =
    nodes.length > 1 ? `Runs in parallel · ${nodes.length} steps` : "Step";
  return (
    <div className="min-w-0 space-y-2">
      <h2
        id={headingId}
        className="font-medium text-muted text-xs uppercase leading-5 tracking-wide"
      >
        {label}
      </h2>
      <ol aria-labelledby={headingId} className="min-w-0 space-y-2">
        {nodes.map((n) => (
          <NodeCard
            key={n.id}
            node={n}
            step={stepOf.get(n.id) ?? 0}
            deps={n.dependsOn
              .map((d) => stepOf.get(d))
              .filter((s): s is number => s !== undefined)}
            live={live}
          />
        ))}
      </ol>
    </div>
  );
}

function PlanWaves({ nodes, live }: { nodes: NodeViewState[]; live: boolean }) {
  const waves = useMemo(() => {
    const byId = new Map(nodes.map((n) => [n.id, n]));
    return planWaves(nodes).map((ids) =>
      ids
        .map((id) => byId.get(id))
        .filter((n): n is NodeViewState => n !== undefined),
    );
  }, [nodes]);
  const stepOf = new Map(nodes.map((n, i) => [n.id, i + 1]));
  return (
    <section aria-label="Plan steps" className="min-w-0 space-y-3">
      <p className="text-muted text-xs leading-5">
        {plural(nodes.length, "step", "steps")} in{" "}
        {plural(waves.length, "wave", "waves")}, top to bottom in dependency
        order.
      </p>
      <div className="min-w-0">
        {waves.map((wave, i) => (
          <div key={wave[0]?.id ?? i} className="min-w-0">
            {i > 0 && <Connector />}
            <Wave nodes={wave} stepOf={stepOf} live={live} />
          </div>
        ))}
      </div>
    </section>
  );
}

export interface PlanTabProps {
  state: RunViewState | null;
  /** The stream is connected and the run unfinished: show live activity. */
  live?: boolean;
}

/** The run's plan as vertical dependency waves, or why there isn't one. */
export function PlanTab({ state, live = false }: PlanTabProps) {
  if (!state?.planned) {
    return (
      <div className="grid min-h-40 place-items-center gap-2 rounded-xl bg-chip px-4 text-center">
        <p className="text-muted text-sm">
          {state?.status === "failed"
            ? "The run stopped before a plan was made."
            : "Planning… the steps appear once the profile is read."}
        </p>
      </div>
    );
  }
  if (state.noLlmWork || state.nodes.length === 0) {
    return (
      <div className="grid min-h-40 place-items-center rounded-xl bg-chip px-4 py-6 text-center">
        <div className="space-y-1">
          <p className="font-medium text-sm">
            Rules fixed everything — no plan needed
          </p>
          <p className="text-muted text-xs">
            {state.ruleActions.length > 0
              ? `${state.ruleActions.length} deterministic ${state.ruleActions.length === 1 ? "fix" : "fixes"}, no model calls.`
              : "No model calls were made."}
          </p>
        </div>
      </div>
    );
  }
  return <PlanWaves nodes={state.nodes} live={live && !state.finished} />;
}
