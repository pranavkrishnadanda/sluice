"use client";

import { useId, useMemo } from "react";
import type { ModelTier } from "@/lib/api/types";
import type { NodeStatus, NodeViewState, RunViewState } from "@/lib/run-state";
import { layoutPlan, PLAN_NODE_HEIGHT, PLAN_VIEW_WIDTH } from "./plan-layout";

/** Extra height for a node whose title wraps onto a second line. */
const SECOND_LINE = 16;

const TIER_LABEL: Record<ModelTier, string> = {
  rules: "rules",
  small: "small model",
  strong: "strong model",
};

const STATUS_LABEL: Record<NodeStatus, string> = {
  pending: "waiting",
  running: "running",
  retrying: "retrying",
  done: "done",
  failed: "failed",
};

/** Approximate glyphs that fit in `width` px of 12.5 px Inter. */
const budget = (width: number) => Math.max(8, Math.floor((width - 24) / 6.4));

function clip(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

/** Word-wraps `text` into at most two lines of `max` glyphs (SVG has no wrap). */
export function wrapTitle(text: string, max: number): [string, string?] {
  if (text.length <= max) return [text];
  const cut = text.lastIndexOf(" ", max);
  const head = cut > max / 2 ? text.slice(0, cut) : text.slice(0, max);
  const rest = text.slice(head.length).trimStart();
  return [head, clip(rest, max)];
}

function nodeMeta(n: NodeViewState): string {
  const attempt = n.attempt > 1 ? ` · attempt ${n.attempt}` : "";
  return `${TIER_LABEL[n.tier]} · ${STATUS_LABEL[n.status]}${attempt}`;
}

function PlanGraph({ nodes }: { nodes: NodeViewState[] }) {
  const arrowId = useId();
  const byId = new Map(nodes.map((n) => [n.id, n]));
  // Widths do not depend on height: lay out once, then grow every node by a
  // line if any title needs two, so bands stay aligned.
  const { layout, twoLine } = useMemo(() => {
    const titles = new Map(nodes.map((n) => [n.id, n.title]));
    const first = layoutPlan(nodes);
    const wraps = first.nodes.some(
      (p) => (titles.get(p.id)?.length ?? 0) > budget(p.width),
    );
    return wraps
      ? {
          layout: layoutPlan(nodes, PLAN_NODE_HEIGHT + SECOND_LINE),
          twoLine: true,
        }
      : { layout: first, twoLine: false };
  }, [nodes]);
  const stepOf = new Map(nodes.map((n, i) => [n.id, i + 1]));
  const fits = layout.width <= PLAN_VIEW_WIDTH;

  return (
    <figure className="space-y-3">
      <div className="overflow-x-auto overscroll-x-contain">
        <svg
          aria-hidden="true"
          focusable="false"
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          width={fits ? "100%" : layout.width}
          className="block h-auto"
        >
          <defs>
            <marker
              id={arrowId}
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 9 5 L 0 9 z" className="fill-(--text-muted)" />
            </marker>
          </defs>
          {layout.edges.map((e) => (
            <path
              key={`${e.from}->${e.to}`}
              data-edge={`${e.from}->${e.to}`}
              d={e.d}
              fill="none"
              strokeWidth="1.5"
              markerEnd={`url(#${arrowId})`}
              className="stroke-(--text-muted)"
            />
          ))}
          {layout.nodes.map((p) => {
            const n = byId.get(p.id);
            if (!n) return null;
            return (
              <g
                key={p.id}
                data-node-id={p.id}
                data-layer={p.layer}
                data-status={n.status}
                className="plan-node"
                transform={`translate(${p.x} ${p.y})`}
              >
                <title>{`${n.title} (${nodeMeta(n)})`}</title>
                <rect
                  width={p.width}
                  height={p.height}
                  rx="12"
                  strokeWidth="1.5"
                />
                <text
                  x="12"
                  y="18"
                  className="fill-(--text-muted) font-mono text-[10.5px]"
                >
                  {`${stepOf.get(p.id) ?? ""} · ${p.id}`}
                </text>
                <text className="fill-(--text) font-medium text-[12.5px]">
                  {wrapTitle(n.title, budget(p.width)).map((line, i) => (
                    <tspan
                      key={i === 0 ? "title" : "title-2"}
                      x="12"
                      y={35 + i * SECOND_LINE}
                    >
                      {line}
                    </tspan>
                  ))}
                </text>
                <text
                  x="12"
                  y={twoLine ? 52 + SECOND_LINE : 52}
                  className="fill-(--text-muted) text-[11px]"
                >
                  {clip(nodeMeta(n), budget(p.width) + 2)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <figcaption className="text-muted text-xs leading-5">
        {nodes.length} {nodes.length === 1 ? "step" : "steps"}, top to bottom in
        dependency order.
      </figcaption>
      {/* The graph's text alternative: every step, its tier, status and inputs. */}
      <ol aria-label="Plan steps" className="sr-only">
        {nodes.map((n, i) => {
          const deps = n.dependsOn
            .map((d) => stepOf.get(d))
            .filter((s): s is number => s !== undefined);
          return (
            <li key={n.id}>
              {`Step ${i + 1}: ${n.title} — ${nodeMeta(n)}. `}
              {deps.length === 0
                ? "No dependencies."
                : `Runs after step ${deps.join(", step ")}.`}
            </li>
          );
        })}
      </ol>
    </figure>
  );
}

/** The run's plan as a layered DAG, or why there isn't one. */
export function PlanTab({ state }: { state: RunViewState | null }) {
  if (!state?.planned) {
    return (
      <div className="grid min-h-40 place-items-center gap-2 rounded-xl bg-chip px-4 text-center">
        <p className="text-muted text-sm">
          {state?.status === "failed"
            ? "The run stopped before a plan was made."
            : "Planning… the step graph appears once the profile is read."}
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
  return <PlanGraph nodes={state.nodes} />;
}
