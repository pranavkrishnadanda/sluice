# Sluice API contract (v1)

This is the hand-off contract between the Sluice UI (`frontend/`) and the
FastAPI backend (`backend/`). The TypeScript source of truth is
[`frontend/src/lib/api/types.ts`](../../frontend/src/lib/api/types.ts); the
backend's Pydantic models must mirror it field for field.

## Conventions

- **JSON is camelCase** (`fileName`, `costUsd`, `rowsQuarantined`, ...). The
  backend's Pydantic models must serialise with these exact names (for example
  `alias_generator=to_camel` with `populate_by_name=True`, and responses
  dumped `by_alias=True`).
- Timestamps (`createdAt`, `at`, `generatedAt`) are ISO-8601 strings in UTC,
  e.g. `2026-10-27T10:14:03.000Z`.
- Money is US dollars as a JSON number (`costUsd`, `costPer1kRowsUsd`).
- Fractions (`cellAccuracy`, `fabricationRate`, `quality`, `passHat4`) are in
  `[0, 1]`; the UI formats them as percentages.
- `null` in an eval field means "not measured", never zero.
- Errors are non-2xx responses; the body is free text or FastAPI's
  `{"detail": ...}`. The UI surfaces the status and body.

## Types

```ts
export type TableName = "orders" | "lineitem";
export type RunStatus = "queued" | "running" | "succeeded" | "quarantined" | "failed";
export type ModelTier = "rules" | "small" | "strong";
export type IssueKind =
  | "rename" | "type" | "unit" | "timezone" | "date_format" | "format" | "encoding"
  | "whitespace" | "case" | "destroyed" | "duplicates" | "foreign_key"
  | "missing_column" | "extra_column";

export interface RunSummary {
  id: string; fileName: string; table: TableName; rows: number;
  status: RunStatus; createdAt: string; costUsd: number;
}
export interface Issue { column: string; kind: IssueKind; detail: string }
export interface PlanNode {
  id: string; title: string; columns: string[]; tier: ModelTier; dependsOn: string[];
}
interface EventBase { seq: number; at: string } // seq strictly increasing from 1 per run; ISO-8601
export type RunEvent = EventBase & (
  | { type: "run_started"; runId: string; fileName: string; table: TableName; rows: number }
  | { type: "profiled"; issues: Issue[] }
  | { type: "rules_applied"; actions: string[] }
  | { type: "plan_created"; nodes: PlanNode[] }
  | { type: "node_started"; nodeId: string; attempt: number }
  | { type: "attempt_failed"; nodeId: string; attempt: number; maxAttempts: number; error: string;
      tokens: number; costUsd: number }
  | { type: "node_finished"; nodeId: string; attempt: number; tier: ModelTier; tokens: number;
      costUsd: number; latencyMs: number; transformId: string }
  | { type: "verified"; checksPassed: number; checksTotal: number }
  | { type: "loaded"; rowsLoaded: number; rowsQuarantined: number; snapshotId: string }
  | { type: "run_finished"; status: Exclude<RunStatus, "queued" | "running">; summary: string;
      costUsd: number; tokens: number }
);
export interface Transform {
  id: string; nodeId: string; attempt: number; language: "polars" | "sql"; code: string;
  passed: boolean;
}
export interface PreviewCell { before: string | null; after: string | null; flagged: boolean }
export interface DataPreview {
  columns: string[];
  rows: { pk: string; cells: Record<string, PreviewCell> }[];
  totalRows: number;
}
export interface CI { mean: number; lo: number; hi: number }
export interface EvalSummary {
  split: "dev" | "holdout"; n: number; generatedAt: string;
  systems: { system: string; cellAccuracy: CI | null; fabricationRate: CI | null;
             passHat4: number | null; costPer1kRowsUsd: CI | null }[];
  pareto: { system: string; kind: "baseline" | "router";
            points: { costPer1kRowsUsd: number; quality: number; lambda?: number }[] }[];
}
export interface ApiClient {
  readonly isDemo: boolean;
  listRuns(): Promise<RunSummary[]>;
  createRun(file: File, table: TableName): Promise<RunSummary>;
  streamEvents(runId: string, onEvent: (e: RunEvent) => void, signal: AbortSignal): Promise<void>;
  getTransforms(runId: string): Promise<Transform[]>;
  getPreview(runId: string, limit: number): Promise<DataPreview>;
  getEvalSummary(): Promise<EvalSummary>;
}
```

## HTTP mapping

Base URL: `NEXT_PUBLIC_SLUICE_API_URL` (no trailing slash needed). When it is
unset or `"mock"`, the UI uses the in-browser `MockApiClient` and shows a
"Demo data" badge.

| `ApiClient` method | Request | Response |
| --- | --- | --- |
| `listRuns()` | `GET /runs` | `200` `RunSummary[]`, newest first |
| `createRun(file, table)` | `POST /runs`, `multipart/form-data` with fields `file` (the CSV) and `table` (`TableName`) | `201` `RunSummary` (status `queued`) |
| `streamEvents(runId, onEvent, signal)` | `GET /runs/{id}/events` (`Accept: text/event-stream`) | `200` SSE stream of `RunEvent`, see below |
| `getTransforms(runId)` | `GET /runs/{id}/transforms` | `200` `Transform[]`, ordered by `nodeId`, then `attempt` |
| `getPreview(runId, limit)` | `GET /runs/{id}/preview?limit=<n>` | `200` `DataPreview` with at most `limit` rows |
| `getEvalSummary()` | `GET /evals/summary` | `200` `EvalSummary` |

Path parameters are URL-encoded. Unknown run ids return `404`.

## Event stream (SSE)

`GET /runs/{id}/events` is a standard `text/event-stream`:

```text
id: 1
data: {"seq":1,"at":"2026-10-27T10:14:03.000Z","type":"run_started","runId":"run-repaired","fileName":"orders_2026-10.csv","table":"orders","rows":48210}

id: 2
data: {"seq":2,"at":"2026-10-27T10:14:03.910Z","type":"profiled","issues":[...]}

```

- Each message carries `id: <seq>` and one `data:` line holding the
  `RunEvent` JSON. Messages are unnamed (no `event:` field).
- `seq` starts at `1` for every run and is strictly increasing with no gaps.
- The stream replays the run's full history from `seq 1` on connect, then
  continues live. After `run_finished` the server closes the stream.
- **Reconnect:** the client resends the last `seq` it applied as the
  `Last-Event-ID` header; the server resumes with events whose `seq` is
  greater than it. The client also drops any event with `seq <=` the last one
  it applied, so a replayed duplicate is harmless.
- Comment lines (`: keep-alive`) may be sent to keep proxies from timing out.

### Event order

A run emits, in order: `run_started`, `profiled`, `rules_applied`,
`plan_created`, then for each plan node `node_started` followed by either
`node_finished` or `attempt_failed` (which, while `attempt < maxAttempts`, is
followed by a `node_started` for the next attempt), then `verified`,
`loaded`, and finally `run_finished`. An empty `plan_created.nodes` means
deterministic rules handled everything and no model was called.

`node_finished.tier` is the tier that produced the accepted transform, which
may be higher than the planned `PlanNode.tier` after escalation.
`attempt_failed.tokens` / `costUsd` are the spend of that failed attempt.
`run_finished.costUsd` / `tokens` are the run totals, including failed
attempts: they equal the sum of every `node_finished` and `attempt_failed`
`tokens` / `costUsd` in the run.
