/**
 * The UI <-> backend contract. Mirrored by the FastAPI backend's Pydantic
 * models (camelCase JSON). See `docs/api/contract.md` before changing.
 */

/** Server maximum for `GET /runs/{id}/preview?limit=`. */
export const PREVIEW_MAX_LIMIT = 1_000;
/** Server maximum upload size for `POST /runs` (100 MB), in bytes. */
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
/** `attempt_failed.error` is truncated to this many characters. */
export const MAX_ATTEMPT_ERROR_CHARS = 2_000;

export type TableName = "orders" | "lineitem";
/**
 * `POST /runs` `table` field: a table, or `"auto"` to let the server pick it
 * from the file's column names (422 `table_unknown` when that is unclear).
 */
export type TableChoice = TableName | "auto";
export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "quarantined"
  | "failed";
export type ModelTier = "rules" | "small" | "strong";
export type IssueKind =
  | "rename"
  | "type"
  | "unit"
  | "timezone"
  | "date_format"
  | "format"
  | "encoding"
  | "whitespace"
  | "case"
  | "destroyed"
  | "duplicates"
  | "foreign_key"
  | "missing_column"
  | "extra_column";
/** What a run is busy with right now (`activity.phase`). */
export type ActivityPhase =
  | "mapping"
  | "planning"
  | "writing"
  | "sandbox"
  | "checking"
  | "verifying"
  | "waiting"
  | "loading"
  | "previewing";
/** `activity.message` is at most this many characters. */
export const MAX_ACTIVITY_MESSAGE_CHARS = 200;

export interface RunSummary {
  id: string;
  fileName: string;
  table: TableName;
  /** `null` until the file has been profiled (and for demo replays). */
  rows: number | null;
  status: RunStatus;
  createdAt: string;
  costUsd: number;
}
export interface Issue {
  column: string;
  kind: IssueKind;
  detail: string;
}
export interface PlanNode {
  id: string;
  title: string;
  columns: string[];
  tier: ModelTier;
  dependsOn: string[];
}
interface EventBase {
  seq: number;
  at: string;
} // seq strictly increasing from 1 per run; ISO-8601
export type RunEvent = EventBase &
  (
    | {
        type: "run_started";
        runId: string;
        fileName: string;
        table: TableName;
        /** `null` while the row count is not known yet. */
        rows: number | null;
      }
    | { type: "profiled"; issues: Issue[] }
    | { type: "rules_applied"; actions: string[] }
    | { type: "plan_created"; nodes: PlanNode[] }
    | {
        type: "node_started";
        nodeId: string;
        attempt: number;
        /** Attempt budget for this node, known from its first attempt. */
        maxAttempts: number;
        /**
         * The tier this attempt runs on (a retry may escalate). Absent or
         * `null` on runs stored before the field existed: use the planned tier.
         */
        tier?: ModelTier | null;
      }
    | {
        type: "attempt_failed";
        nodeId: string;
        attempt: number;
        maxAttempts: number;
        /** At most {@link MAX_ATTEMPT_ERROR_CHARS} characters. */
        error: string;
        /** Spend of the failed attempt; counted in `run_finished` totals. */
        tokens: number;
        costUsd: number;
      }
    | {
        type: "node_finished";
        nodeId: string;
        attempt: number;
        tier: ModelTier;
        tokens: number;
        costUsd: number;
        latencyMs: number;
        transformId: string;
      }
    | { type: "verified"; checksPassed: number; checksTotal: number }
    | {
        type: "loaded";
        rowsLoaded: number;
        rowsQuarantined: number;
        snapshotId: string;
      }
    | {
        type: "run_finished";
        status: Exclude<RunStatus, "queued" | "running">;
        summary: string;
        costUsd: number;
        tokens: number;
      }
    | {
        /** Emitted when a phase starts; never a periodic heartbeat. */
        type: "activity";
        /** The plan node, or `null` for run-wide phases. */
        nodeId: string | null;
        phase: ActivityPhase;
        /** Short user-facing copy; never data values, prompts or paths. */
        message: string;
        tier: ModelTier | null;
        attempt: number | null;
        /** Rate-limit wait in whole seconds, set only for `waiting`. */
        retryInS: number | null;
      }
  );
export interface Transform {
  id: string;
  nodeId: string;
  attempt: number;
  language: "polars" | "sql";
  code: string;
  passed: boolean;
}
export interface PreviewCell {
  before: string | null;
  after: string | null;
  flagged: boolean;
}
export interface PreviewRow {
  /** Primary key; composite keys join their parts with "|" ("1|3"). */
  pk: string;
  /** The whole row was held back from the load. */
  quarantined: boolean;
  /** Why it was quarantined; `null` when `quarantined` is false. */
  reason: string | null;
  /** Keyed by target contract column. */
  cells: Record<string, PreviewCell>;
}
export interface DataPreview {
  /** Target contract columns, in contract order. */
  columns: string[];
  rows: PreviewRow[];
  /** Rows in the uploaded file. */
  totalRows: number;
  /** `offset` for the next page; `null` after the last page. */
  nextOffset: number | null;
}
/** One page of `GET /runs/{id}/input`: the uploaded file's raw rows. */
export interface RawInput {
  /** The file's own column names (nested JSON flattened), not the contract's. */
  columns: string[];
  /** One array per row, in `columns` order; `null` for an empty cell. */
  rows: (string | null)[][];
  /** Rows in the uploaded file. */
  totalRows: number;
  /** `offset` for the next page; `null` after the last page. */
  nextOffset: number | null;
}
export interface PreviewPage {
  offset: number;
  /** Capped by the server at {@link PREVIEW_MAX_LIMIT}. */
  limit: number;
}
export interface CI {
  mean: number;
  lo: number;
  hi: number;
}
export interface EvalSummary {
  /** `ood` (out-of-distribution) may be added later. */
  split: "dev" | "holdout" | "ood";
  /** Number of eval cases. */
  n: number;
  generatedAt: string;
  systems: {
    system: string;
    cellAccuracy: CI | null;
    fabricationRate: CI | null;
    /** Share of cases whose output passes every contract check. */
    contractPassRate: CI | null;
    /** Estimated probability that all 4 runs of a case succeed. */
    passHat4: CI | null;
    p95LatencyMs: CI | null;
    costPer1kRowsUsd: CI | null;
  }[];
  pareto: {
    system: string;
    kind: "baseline" | "router";
    points: {
      costPer1kRowsUsd: number;
      /** Cell accuracy at this operating point. */
      quality: number;
      /** Router trade-off weight; `null` for baselines. */
      lambda: number | null;
    }[];
  }[];
}
export interface ApiClient {
  readonly isDemo: boolean;
  listRuns(): Promise<RunSummary[]>;
  createRun(file: File, table: TableName): Promise<RunSummary>;
  streamEvents(
    runId: string,
    onEvent: (e: RunEvent) => void,
    signal: AbortSignal,
  ): Promise<void>;
  getTransforms(runId: string): Promise<Transform[]>;
  getPreview(runId: string, page: PreviewPage): Promise<DataPreview>;
  getEvalSummary(): Promise<EvalSummary>;
  /**
   * Demo only, never part of the HTTP API: `true` when `runId` was created
   * through the mock, which cannot read the uploaded file and replays a
   * sample run instead. Absent on real clients.
   */
  isSampleReplay?(runId: string): boolean;
}

/** Convenience: a single event variant by its `type`. */
export type RunEventOf<T extends RunEvent["type"]> = Extract<
  RunEvent,
  { type: T }
>;
