/**
 * The UI <-> backend contract. Mirrored by the FastAPI backend's Pydantic
 * models (camelCase JSON). See `docs/api/contract.md` before changing.
 */

export type TableName = "orders" | "lineitem";
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

export interface RunSummary {
  id: string;
  fileName: string;
  table: TableName;
  rows: number;
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
        rows: number;
      }
    | { type: "profiled"; issues: Issue[] }
    | { type: "rules_applied"; actions: string[] }
    | { type: "plan_created"; nodes: PlanNode[] }
    | { type: "node_started"; nodeId: string; attempt: number }
    | {
        type: "attempt_failed";
        nodeId: string;
        attempt: number;
        maxAttempts: number;
        error: string;
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
export interface DataPreview {
  columns: string[];
  rows: { pk: string; cells: Record<string, PreviewCell> }[];
  totalRows: number;
}
export interface CI {
  mean: number;
  lo: number;
  hi: number;
}
export interface EvalSummary {
  split: "dev" | "holdout";
  n: number;
  generatedAt: string;
  systems: {
    system: string;
    cellAccuracy: CI | null;
    fabricationRate: CI | null;
    passHat4: number | null;
    costPer1kRowsUsd: CI | null;
  }[];
  pareto: {
    system: string;
    kind: "baseline" | "router";
    points: { costPer1kRowsUsd: number; quality: number; lambda?: number }[];
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
  getPreview(runId: string, limit: number): Promise<DataPreview>;
  getEvalSummary(): Promise<EvalSummary>;
}

/** Convenience: a single event variant by its `type`. */
export type RunEventOf<T extends RunEvent["type"]> = Extract<
  RunEvent,
  { type: T }
>;
