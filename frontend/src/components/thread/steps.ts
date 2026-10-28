import { formatCount, formatMs, formatUsd } from "@/lib/format";
import type { NodeViewState, RunViewState } from "@/lib/run-state";

/**
 * How a step row reads. `active` is the in-flight step (pulsing dot);
 * `pending` is a planned node that has not started.
 */
export type StepStatus = "done" | "active" | "retrying" | "failed" | "pending";

export interface Step {
  /** Stable per milestone or plan node, so rows animate in only once. */
  id: string;
  status: StepStatus;
  /** Lead text, e.g. "Profiled" or a plan node's title. */
  label: string;
  /** Inline after the label: "Profiled — 4 issues found". */
  detail?: string;
  /** Warn-tinted detail (quarantined rows, failed checks). */
  detailWarn?: boolean;
  /** Second line, tinted by status: "attempt 2/3" or "$0.02 · 2.1 s". */
  meta?: string;
  /** Second line, muted: the last error, or "after 1 retry". */
  note?: string;
  /** Right-aligned chip: model tier or "rules · $0". */
  chip?: string;
}

/** Screen-reader status prefix for each row (status is never icon-only). */
export const STEP_STATUS_TEXT: Record<StepStatus, string> = {
  done: "Done",
  active: "In progress",
  retrying: "Retrying",
  failed: "Failed",
  pending: "Queued",
};

const RULES_CHIP = `rules · ${formatUsd(0)}`;

function plural(n: number, one: string, many: string): string {
  return `${formatCount(n)} ${n === 1 ? one : many}`;
}

function nodeStep(node: NodeViewState): Step {
  const base = { id: `node-${node.id}`, label: node.title, chip: node.tier };
  const of = node.maxAttempts ? `/${node.maxAttempts}` : "";
  switch (node.status) {
    case "pending":
      return { ...base, status: "pending" };
    case "running":
      return { ...base, status: "active", meta: "Writing a transform…" };
    case "retrying":
      return {
        ...base,
        status: "retrying",
        meta: `attempt ${node.attempt}${of}`,
        note: node.lastError ?? undefined,
      };
    case "failed":
      return {
        ...base,
        status: "failed",
        meta: `attempt ${node.attempt}${of}`,
        note: node.lastError ?? undefined,
      };
    case "done": {
      const retries = node.attempt - 1;
      return {
        ...base,
        status: "done",
        meta: `${formatUsd(node.costUsd)} · ${formatMs(node.latencyMs ?? 0)}`,
        note:
          retries > 0
            ? `after ${retries} ${retries === 1 ? "retry" : "retries"}`
            : undefined,
      };
    }
  }
}

/**
 * The thread's step rows, in pipeline order: Profiled, Rules, one row per
 * plan node (or "No LLM work needed"), Verified, Loaded.
 *
 * While `live` (the stream is connected and the run has not finished), the
 * next milestone is shown as an `active` row so there is always visible
 * progress between events. Pure: derived from {@link RunViewState} only.
 */
export function buildSteps(state: RunViewState, live: boolean): Step[] {
  if (!state.runId) return [];
  const steps: Step[] = [];
  const next = (step: Omit<Step, "status">) => {
    if (live && !state.finished) steps.push({ ...step, status: "active" });
    return steps;
  };

  if (!state.profiled) {
    const rows = state.rows == null ? "" : ` ${formatCount(state.rows)} rows`;
    return next({ id: "profiled", label: `Profiling${rows}…` });
  }
  steps.push({
    id: "profiled",
    status: "done",
    label: "Profiled",
    detail:
      state.issues.length === 0
        ? "no issues found"
        : `${plural(state.issues.length, "issue", "issues")} found`,
  });

  if (!state.rulesApplied) {
    return next({ id: "rules", label: "Applying rules…", chip: RULES_CHIP });
  }
  steps.push({
    id: "rules",
    status: "done",
    label: "Rules",
    detail: plural(state.ruleActions.length, "fix", "fixes"),
    chip: RULES_CHIP,
  });

  if (!state.planned) return next({ id: "plan", label: "Planning repairs…" });
  if (state.noLlmWork) {
    steps.push({
      id: "plan",
      status: "done",
      label: "No LLM work needed",
      detail: "rules handled every issue",
      chip: RULES_CHIP,
    });
  }
  const nodeSteps = state.nodes.map(nodeStep);
  // Between a node finishing and the next starting, show the next one as
  // in flight so a live run never looks stalled.
  const idle = !nodeSteps.some(
    (s) => s.status !== "done" && s.status !== "pending",
  );
  const upNext = nodeSteps.find((s) => s.status === "pending");
  if (live && !state.finished && idle && upNext) upNext.status = "active";
  steps.push(...nodeSteps);

  if (state.nodes.some((n) => n.status !== "done")) return steps;
  const table = state.table ? ` the ${state.table}` : " the";
  if (!state.verified) {
    return next({
      id: "verified",
      label: `Verifying against${table} contract…`,
    });
  }
  const { checksPassed, checksTotal } = state.verified;
  steps.push({
    id: "verified",
    status: "done",
    label: "Verified",
    detail: `${checksPassed}/${checksTotal} contract checks`,
    detailWarn: checksPassed < checksTotal,
  });

  if (!state.loaded) {
    const rows = state.rows == null ? "" : ` ${formatCount(state.rows)} rows`;
    return next({ id: "loaded", label: `Loading${rows}…` });
  }
  const { rowsLoaded, rowsQuarantined } = state.loaded;
  steps.push({
    id: "loaded",
    status: "done",
    label: `Loaded ${formatCount(rowsLoaded)} rows`,
    detail:
      rowsQuarantined > 0
        ? `${formatCount(rowsQuarantined)} quarantined`
        : undefined,
    detailWarn: rowsQuarantined > 0,
  });
  return steps;
}

/**
 * One sentence for the polite live region: the newest settled (or
 * retrying) step, or the run's outcome. Announcing only the change keeps a
 * screen reader from re-reading the whole list on every event.
 */
export function announceLatest(state: RunViewState, steps: Step[]): string {
  if (state.finished) return `Run ${state.status}.`;
  const latest = steps.findLast(
    (s) => s.status !== "active" && s.status !== "pending",
  );
  if (!latest) return state.runId ? "Run started." : "";
  const parts = [latest.label, latest.detail, latest.meta, latest.note].filter(
    Boolean,
  );
  return `${STEP_STATUS_TEXT[latest.status]}: ${parts.join(", ")}.`;
}
