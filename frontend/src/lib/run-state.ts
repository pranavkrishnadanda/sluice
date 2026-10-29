import {
  type ActivityPhase,
  type Issue,
  MAX_ATTEMPT_ERROR_CHARS,
  type ModelTier,
  type PlanNode,
  type RunEvent,
  type RunEventOf,
  type RunStatus,
  type TableName,
} from "@/lib/api/types";

/**
 * The latest `activity` event: what is being performed right now. The UI
 * ticks the elapsed time (and a `waiting` countdown) from `at`.
 */
export interface ActivityView {
  seq: number;
  /** When the phase started (the event's `at`). */
  at: string;
  phase: ActivityPhase;
  message: string;
  tier: ModelTier | null;
  attempt: number | null;
  retryInS: number | null;
}

export type NodeStatus = "pending" | "running" | "retrying" | "done" | "failed";

export interface NodeViewState {
  id: string;
  title: string;
  columns: string[];
  dependsOn: string[];
  status: NodeStatus;
  /** Current (or last) attempt number; 0 before the node starts. */
  attempt: number;
  /**
   * Highest attempt that has produced a result (`attempt_failed` or
   * `node_finished`); 0 until then. Unlike `attempt`, a `node_started`
   * does not move it, so it is the count of attempts with a transform.
   */
  attemptsReported: number;
  /** Attempt budget, known from the node's first `node_started`. */
  maxAttempts: number | null;
  /** Planned tier, replaced by the tier that actually produced the result. */
  tier: ModelTier;
  /** Spend summed over every attempt of this node, failed ones included. */
  costUsd: number;
  tokens: number;
  latencyMs: number | null;
  lastError: string | null;
  transformId: string | null;
  /** What this node is doing right now; `null` between phases. */
  activity: ActivityView | null;
}

export interface RunViewState {
  /** Highest `seq` applied; events at or below it are ignored. */
  lastSeq: number;
  runId: string | null;
  fileName: string | null;
  table: TableName | null;
  rows: number | null;
  status: RunStatus;
  /** `true` once `profiled` has arrived (it may report zero issues). */
  profiled: boolean;
  issues: Issue[];
  /** `true` once `rules_applied` has arrived (it may report zero actions). */
  rulesApplied: boolean;
  ruleActions: string[];
  /** `true` once `plan_created` has arrived. */
  planned: boolean;
  nodes: NodeViewState[];
  /** The plan had zero nodes: deterministic rules handled everything. */
  noLlmWork: boolean;
  verified: { checksPassed: number; checksTotal: number } | null;
  loaded: {
    rowsLoaded: number;
    rowsQuarantined: number;
    snapshotId: string;
  } | null;
  /**
   * Running totals: spend from every attempt (`attempt_failed` and
   * `node_finished`) plus accepted-attempt latency; cost and tokens are
   * replaced by the authoritative `run_finished` figures.
   */
  totals: { costUsd: number; tokens: number; latencyMs: number };
  summary: string | null;
  /**
   * Run-wide activity (an `activity` with no `nodeId`), e.g. planning or
   * loading; cleared by any later non-activity event.
   */
  activity: ActivityView | null;
  finished: boolean;
  startedAt: string | null;
  finishedAt: string | null;
}

export const INITIAL_RUN_VIEW_STATE: RunViewState = {
  lastSeq: 0,
  runId: null,
  fileName: null,
  table: null,
  rows: null,
  status: "queued",
  profiled: false,
  issues: [],
  rulesApplied: false,
  ruleActions: [],
  planned: false,
  nodes: [],
  noLlmWork: false,
  verified: null,
  loaded: null,
  totals: { costUsd: 0, tokens: 0, latencyMs: 0 },
  summary: null,
  activity: null,
  finished: false,
  startedAt: null,
  finishedAt: null,
};

function toNode(node: PlanNode): NodeViewState {
  return {
    id: node.id,
    title: node.title,
    columns: [...node.columns],
    dependsOn: [...node.dependsOn],
    status: "pending",
    attempt: 0,
    attemptsReported: 0,
    maxAttempts: null,
    tier: node.tier,
    costUsd: 0,
    tokens: 0,
    latencyMs: null,
    lastError: null,
    transformId: null,
    activity: null,
  };
}

function updateNode(
  state: RunViewState,
  nodeId: string,
  patch: (node: NodeViewState) => Partial<NodeViewState>,
): NodeViewState[] {
  return state.nodes.map((n) => (n.id === nodeId ? { ...n, ...patch(n) } : n));
}

function onNodeFinished(
  state: RunViewState,
  e: RunEventOf<"node_finished">,
): Partial<RunViewState> {
  const known = state.nodes.some((n) => n.id === e.nodeId);
  return {
    nodes: updateNode(state, e.nodeId, (n) => ({
      status: "done",
      attempt: e.attempt,
      attemptsReported: Math.max(n.attemptsReported, e.attempt),
      tier: e.tier,
      costUsd: n.costUsd + e.costUsd,
      tokens: n.tokens + e.tokens,
      latencyMs: e.latencyMs,
      lastError: null,
      transformId: e.transformId,
      activity: null,
    })),
    totals: known
      ? {
          costUsd: state.totals.costUsd + e.costUsd,
          tokens: state.totals.tokens + e.tokens,
          latencyMs: state.totals.latencyMs + e.latencyMs,
        }
      : state.totals,
  };
}

function onAttemptFailed(
  state: RunViewState,
  e: RunEventOf<"attempt_failed">,
): Partial<RunViewState> {
  const known = state.nodes.some((n) => n.id === e.nodeId);
  return {
    nodes: updateNode(state, e.nodeId, (n) => ({
      status: e.attempt < e.maxAttempts ? "retrying" : "failed",
      attempt: e.attempt,
      attemptsReported: Math.max(n.attemptsReported, e.attempt),
      maxAttempts: e.maxAttempts,
      // The contract caps this; clamp anyway so a long trace can't flood the UI.
      lastError: e.error.slice(0, MAX_ATTEMPT_ERROR_CHARS),
      costUsd: n.costUsd + e.costUsd,
      tokens: n.tokens + e.tokens,
      activity: null,
    })),
    totals: known
      ? {
          ...state.totals,
          costUsd: state.totals.costUsd + e.costUsd,
          tokens: state.totals.tokens + e.tokens,
        }
      : state.totals,
  };
}

/** Shown on a node the run stopped mid-attempt (no result for that attempt). */
export const STOPPED_NODE_NOTE =
  "Stopped: the run ended before this attempt finished.";

/**
 * `run_finished` of any status closes every node still open (contract,
 * "Early end"): a running or retrying node becomes `failed`. A node stopped
 * mid-attempt (its current attempt never reported) gets a stop note; one
 * whose last `attempt_failed` had no follow-up keeps that error. Pending
 * nodes never started, so they stay pending.
 */
function closeOpenNodes(nodes: NodeViewState[]): NodeViewState[] {
  return nodes.map((n) => {
    if (n.status !== "running" && n.status !== "retrying") return n;
    const midAttempt = n.attempt > n.attemptsReported;
    return {
      ...n,
      status: "failed",
      lastError: midAttempt ? STOPPED_NODE_NOTE : n.lastError,
    };
  });
}

function onActivity(
  state: RunViewState,
  e: RunEventOf<"activity">,
): Partial<RunViewState> {
  const view: ActivityView = {
    seq: e.seq,
    at: e.at,
    phase: e.phase,
    message: e.message,
    tier: e.tier,
    attempt: e.attempt,
    retryInS: e.retryInS,
  };
  if (e.nodeId === null) return { activity: view };
  // Unknown node: nothing to attach it to (like other node events).
  if (!state.nodes.some((n) => n.id === e.nodeId)) return {};
  // A newer, node-scoped activity supersedes the run-wide one.
  return {
    activity: null,
    nodes: updateNode(state, e.nodeId, () => ({ activity: view })),
  };
}

function apply(state: RunViewState, e: RunEvent): Partial<RunViewState> {
  switch (e.type) {
    case "run_started":
      return {
        runId: e.runId,
        fileName: e.fileName,
        table: e.table,
        rows: e.rows,
        status: "running",
        startedAt: e.at,
      };
    case "profiled":
      return { profiled: true, issues: [...e.issues] };
    case "rules_applied":
      return { rulesApplied: true, ruleActions: [...e.actions] };
    case "plan_created":
      return {
        planned: true,
        nodes: e.nodes.map(toNode),
        noLlmWork: e.nodes.length === 0,
      };
    case "node_started":
      return {
        nodes: updateNode(state, e.nodeId, (n) => ({
          status: e.attempt > 1 ? "retrying" : "running",
          attempt: e.attempt,
          maxAttempts: e.maxAttempts,
          // The tier this attempt uses (older servers omit it).
          tier: e.tier ?? n.tier,
          // The server may announce an attempt's first phase just before
          // its node_started; only an earlier attempt's activity is stale.
          activity: n.activity?.attempt === e.attempt ? n.activity : null,
        })),
      };
    case "activity":
      return onActivity(state, e);
    case "attempt_failed":
      return onAttemptFailed(state, e);
    case "node_finished":
      return onNodeFinished(state, e);
    case "verified":
      return {
        verified: { checksPassed: e.checksPassed, checksTotal: e.checksTotal },
      };
    case "loaded":
      return {
        loaded: {
          rowsLoaded: e.rowsLoaded,
          rowsQuarantined: e.rowsQuarantined,
          snapshotId: e.snapshotId,
        },
      };
    case "run_finished":
      return {
        status: e.status,
        summary: e.summary,
        finished: true,
        finishedAt: e.at,
        totals: { ...state.totals, costUsd: e.costUsd, tokens: e.tokens },
        nodes: closeOpenNodes(state.nodes).map((n) =>
          n.activity ? { ...n, activity: null } : n,
        ),
      };
    default:
      // Unknown event types are ignored (contract: clients skip them).
      return {};
  }
}

/**
 * Folds one {@link RunEvent} into the run's view state. Pure: never mutates
 * `state`. Duplicate or stale events (`seq <= lastSeq`, e.g. after an SSE
 * reconnect) return `state` unchanged by reference. Events for unknown nodes
 * only advance `lastSeq`.
 */
export function reduceRunEvents(
  state: RunViewState | undefined,
  e: RunEvent,
): RunViewState {
  const current = state ?? INITIAL_RUN_VIEW_STATE;
  if (e.seq <= current.lastSeq) return current;
  // Any later non-activity event ends the run-wide phase it followed.
  const cleared =
    e.type !== "activity" && current.activity
      ? { ...current, activity: null }
      : current;
  return { ...cleared, ...apply(cleared, e), lastSeq: e.seq };
}

/** The newest activity, run-wide or on any node; `null` when idle. */
export function latestActivity(state: RunViewState): ActivityView | null {
  let latest = state.activity;
  for (const n of state.nodes) {
    if (n.activity && (!latest || n.activity.seq > latest.seq)) {
      latest = n.activity;
    }
  }
  return latest;
}
