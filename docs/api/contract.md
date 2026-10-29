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
- Fractions (`cellAccuracy`, `fabricationRate`, `contractPassRate`,
  `passHat4`, `quality`) are in `[0, 1]`; the UI formats them as percentages.
  Latencies are milliseconds (`latencyMs`, `p95LatencyMs`).
- `null` in an eval field means "not measured", never zero.
- **Clients ignore unknown fields and unknown event types**, so the backend
  may add either without a version bump. Removing or retyping a field is a
  breaking change.

### IDs

Run ids, transform ids, plan-node ids and snapshot ids are **opaque**
strings matching `[A-Za-z0-9_-]{1,64}`. Clients never parse them. Transform
ids are **globally unique** (not just per run), and every
`node_finished.transformId` equals the `id` of a `Transform` returned by
`GET /runs/{id}/transforms` for that run.

### Errors

Every 4xx and 5xx response has a JSON body:

```json
{ "detail": "File too large: the limit is 100 MB.", "code": "too_large" }
```

`detail` is always a string written for the user; `code` is optional and
machine-readable. FastAPI's default 422 body (an array of validation
errors) must be overridden with this shape. The UI shows `detail` for 4xx
responses and its own connection copy for network errors and 5xx.

| Status | When |
| --- | --- |
| `403` | A non-`GET` request (e.g. `POST /runs`) whose `Origin` header is not an allowed UI origin: `code` is `forbidden_origin`, answered before the body is read. Requests without `Origin` (curl, scripts) are allowed |
| `404` | Unknown run id (any `/runs/{id}/...` route) |
| `409` | `GET /runs/{id}/preview` when there is no preview to page: `code` is `not_loaded` (the run has not emitted `loaded`), `not_ready` (loaded, the preview is still being written; retry shortly) or `no_preview` (the run finished without a preview). See [Preview](#preview) |
| `413` | Upload larger than **100 MB** |
| `415` | Upload with an unsupported file format |
| `422` | Unknown `table`, a missing form field, or a file that cannot be parsed in its format |
| `422` | `code: "too_large_decoded"`: a `.parquet` upload whose footer declares more than **5,000,000 rows** or more than **1 GiB** of decoded data (checked before the data is read) |

## Base URL, CORS and configuration

- The backend listens on **port 8080** in development
  (`http://localhost:8080`; port 8000 is reserved on the owner's machine).
- The UI reads the base URL from `NEXT_PUBLIC_SLUICE_API_URL` (no trailing
  slash needed). When it is unset or `"mock"`, the UI uses the in-browser
  `MockApiClient` and shows "Demo data" badges.
- **`NEXT_PUBLIC_SLUICE_API_URL` is inlined at build time.** Switching between
  the mock and a backend, or between backends, needs a rebuild (and a
  restart of `next dev`).
- **CORS:** the UI calls the API cross-origin from the browser. The backend
  sends `Access-Control-Allow-Origin` for the UI's origin (e.g.
  `http://localhost:3000`) on **every** route, including the SSE stream and
  error responses, and answers preflight requests allowing the
  `Last-Event-ID` request header. There are no cookies
  (`Access-Control-Allow-Credentials` is not needed) and no auth in v1: the
  API is meant for localhost only. Non-`GET` requests from any other origin
  are refused with `403 forbidden_origin` (see [Errors](#errors)).

## Types

```ts
export const PREVIEW_MAX_LIMIT = 1_000;        // server max for ?limit=
export const MAX_ATTEMPT_ERROR_CHARS = 2_000;  // attempt_failed.error cap

export type TableName = "orders" | "lineitem";
export type RunStatus = "queued" | "running" | "succeeded" | "quarantined" | "failed";
export type ModelTier = "rules" | "small" | "strong";
export type IssueKind =
  | "rename" | "type" | "unit" | "timezone" | "date_format" | "format" | "encoding"
  | "whitespace" | "case" | "destroyed" | "duplicates" | "foreign_key"
  | "missing_column" | "extra_column";

export interface RunSummary {
  id: string; fileName: string; table: TableName;
  rows: number | null;   // null until the file has been profiled
  status: RunStatus; createdAt: string;
  costUsd: number;       // running total while in flight; final once finished
}
export interface Issue { column: string; kind: IssueKind; detail: string }
export interface PlanNode {
  id: string; title: string; columns: string[]; tier: ModelTier; dependsOn: string[];
}
interface EventBase { seq: number; at: string } // seq strictly increasing from 1 per run; ISO-8601
export type RunEvent = EventBase & (
  | { type: "run_started"; runId: string; fileName: string; table: TableName;
      rows: number | null }                        // null while not yet counted
  | { type: "profiled"; issues: Issue[] }
  | { type: "rules_applied"; actions: string[] }
  | { type: "plan_created"; nodes: PlanNode[] }
  | { type: "node_started"; nodeId: string; attempt: number; maxAttempts: number }
  | { type: "attempt_failed"; nodeId: string; attempt: number; maxAttempts: number;
      error: string;                               // at most 2,000 characters
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
export interface PreviewRow {
  pk: string; quarantined: boolean; reason: string | null;
  cells: Record<string, PreviewCell>;
}
export interface DataPreview {
  columns: string[]; rows: PreviewRow[]; totalRows: number; nextOffset: number | null;
}
export interface PreviewPage { offset: number; limit: number }
export interface CI { mean: number; lo: number; hi: number }
export interface EvalSummary {
  split: "dev" | "holdout" | "ood"; n: number; generatedAt: string;
  systems: { system: string; cellAccuracy: CI | null; fabricationRate: CI | null;
             contractPassRate: CI | null; passHat4: CI | null; p95LatencyMs: CI | null;
             costPer1kRowsUsd: CI | null }[];
  pareto: { system: string; kind: "baseline" | "router";
            points: { costPer1kRowsUsd: number; quality: number; lambda: number | null }[] }[];
}
export interface ApiClient {
  readonly isDemo: boolean;
  listRuns(): Promise<RunSummary[]>;
  createRun(file: File, table: TableName): Promise<RunSummary>;
  streamEvents(runId: string, onEvent: (e: RunEvent) => void, signal: AbortSignal): Promise<void>;
  getTransforms(runId: string): Promise<Transform[]>;
  getPreview(runId: string, page: PreviewPage): Promise<DataPreview>;
  getEvalSummary(): Promise<EvalSummary>;
  isSampleReplay?(runId: string): boolean; // demo mock only, not an HTTP route
}
```

## HTTP mapping

| `ApiClient` method | Request | Response |
| --- | --- | --- |
| `listRuns()` | `GET /runs` | `200` `RunSummary[]`, newest first (`createdAt` descending), at most 100 |
| `createRun(file, table)` | `POST /runs`, `multipart/form-data` with fields `file` and `table` (`TableName`) | `201` `RunSummary` (status `queued`, `rows: null`) |
| `streamEvents(runId, onEvent, signal)` | `GET /runs/{id}/events` (`Accept: text/event-stream`) | `200` SSE stream of `RunEvent`, see below |
| `getTransforms(runId)` | `GET /runs/{id}/transforms` | `200` `Transform[]`, see below |
| `getPreview(runId, { offset, limit })` | `GET /runs/{id}/preview?offset=<n>&limit=<n>` | `200` `DataPreview`, see below; `409` before `loaded` |
| `getEvalSummary()` | `GET /evals/summary` | `200` `EvalSummary` |

Path parameters are URL-encoded. Unknown run ids return `404`.

### Upload

- Accepted formats: **`.csv`, `.json`, `.parquet`** (by extension; the server
  may also check the content). Anything else is `415`.
- Size limit: **100 MB**; larger uploads are `413`.
- An unknown `table` or an unparseable file is `422`.
- A `.parquet` file is also `422` (`too_large_decoded`) when its footer
  declares more than 5,000,000 rows or more than 1 GiB of decoded data: a
  small, highly compressed file can expand far beyond 100 MB in memory.
- The new run's `rows` is `null` until the file has been profiled.

### Run summaries

`RunSummary.costUsd` is the **running total** of every attempt's spend while
the run is in flight (it grows with each `attempt_failed` / `node_finished`)
and equals `run_finished.costUsd` once the run has finished.

## Event stream (SSE)

`GET /runs/{id}/events` is a standard `text/event-stream`:

```text
id: 1
data: {"seq":1,"at":"2026-10-27T10:14:03.000Z","type":"run_started","runId":"run-repaired","fileName":"orders_2026-10.csv","table":"orders","rows":48210}

id: 2
data: {"seq":2,"at":"2026-10-27T10:14:03.910Z","type":"profiled","issues":[...]}

```

- Each message carries `id: <seq>` and one `data:` line holding the
  `RunEvent` JSON. Messages are unnamed (no `event:` field). Lines may end
  in LF, CRLF or CR.
- `seq` starts at `1` for every run and is strictly increasing with no gaps.
- **Connect:** the stream replays the run's full history from `seq 1`, then
  continues live. Connecting to a run that has already finished replays its
  history (ending with `run_finished`) and then closes.
- **Close:** after `run_finished` the server closes the stream.
- **Queued runs:** while a run waits to start, the server keeps the
  connection open and sends a comment line (`: keep-alive`) at least every
  **15 seconds**, so proxies and the client never see an idle connection.
  Keep-alive comments may be sent at any other time too.
- **Reconnect (`Last-Event-ID`):** the client resends the last `seq` it
  applied as the `Last-Event-ID` header; the server resumes with the events
  whose `seq` is greater than it, then continues live. The client also drops
  any event with `seq <=` the last one it applied, so a replayed duplicate is
  harmless.
- Clients **ignore unknown event types and unknown fields**; a malformed
  `data:` line is skipped.

### Event order

A run emits, in order: `run_started`, `profiled`, `rules_applied`,
`plan_created`, then for each plan node `node_started` followed by either
`node_finished` or `attempt_failed`. An `attempt_failed` with
`attempt < maxAttempts` is followed by a `node_started` for the next attempt
of the same node, unless the run ends there. Then `verified`, `loaded`, and
finally `run_finished`. An empty `plan_created.nodes` means deterministic rules handled everything
and no model was called.

`node_started.maxAttempts` (equal to `attempt_failed.maxAttempts` for that
node) lets the UI show "attempt 1/3" from the first attempt.

Independent nodes run concurrently, so the events of different nodes may
interleave; the order above holds per node.

**Early end.** A run can end early: a crash, a cancellation, a provider
outage it could not route around, or a spend/time cap. Then a node's last
`attempt_failed` (with `attempt < maxAttempts`) has no following
`node_started`, a `node_started` may have no `node_finished` or
`attempt_failed` at all (the node was stopped mid-attempt), and events of
other nodes that were still running may appear between a node's last event
and `run_finished`. Clients treat `run_finished` as closing every node that
is still open.

**Failure path.** If a node's final attempt fails (`attempt_failed` with
`attempt == maxAttempts`), no further nodes start (nodes already running
finish, and their events are still emitted) and the run does **not** emit
`verified` or `loaded`: it ends with `run_finished` with status `quarantined`
(the input is held back; nothing was written). This takes precedence over
`failed`: if a node's final attempt fails and another node also hits a
provider outage, the run is `quarantined`. Nothing loads either way, and the
quarantine is the more specific signal.

**Exactly one terminal event.** Every run ends with exactly one
`run_finished`, whatever happens: a worker crash, a model-provider outage
or a spend/time cap still produces a `run_finished` (status `failed` or
`quarantined`) with a `summary` saying why, and the run totals so far.

**Statuses.**

- `succeeded`: every check passed and all rows were loaded.
- `quarantined`: the pipeline worked as designed but held data back: some
  rows (`loaded.rowsQuarantined > 0`) or the whole input (no `loaded`) are
  in quarantine for review. Nothing invalid reached the warehouse.
- `failed`: the system could not complete the run (crash, cap, or a
  provider outage it could not route around). It says nothing about the
  data's quality. An outage during schema mapping or planning is not a
  failure: that step falls back to the deterministic rules, the run can
  still succeed, and `run_finished.summary` names the step that fell back.
  When a node's final attempt fails as well, the run is `quarantined`, not
  `failed` (see the failure path).

`node_finished.tier` is the tier that produced the accepted transform, which
may be higher than the planned `PlanNode.tier` after escalation.
`attempt_failed.tokens` / `costUsd` are the spend of that failed attempt.
`attempt_failed.error` is at most **2,000 characters** (truncated by the
server). `run_finished.costUsd` / `tokens` are the full run totals: every
model call in the run, including failed attempts and the calls that belong to
no node event (schema mapping, planning and verification). They are therefore
greater than or equal to the sum of every `node_finished` and
`attempt_failed` `tokens` / `costUsd` in the run.

## Transforms

`GET /runs/{id}/transforms` returns **one `Transform` per attempt that
produced code**, failed attempts included (`passed: false`). An attempt that
produced no code (e.g. a model-provider error) has no transform. Transforms
are ordered by **plan order** (the node's position in `plan_created.nodes`),
then by `attempt`. Nodes on the `rules` tier never have transforms. The
accepted attempt's `id` is its `node_finished.transformId`.

## Preview

`GET /runs/{id}/preview?offset=<n>&limit=<n>` pages through the run's
before/after preview:

- `offset` defaults to `0`; `limit` defaults to, and is capped at, **1,000**
  (`PREVIEW_MAX_LIMIT`).
- `nextOffset` is the `offset` of the next page, or `null` after the last.
- `409` while there is no preview to page, with `code`:
  - `not_loaded`: the run has not emitted `loaded` yet (a run that never
    loads has no preview, so this stays the answer for it);
  - `not_ready`: the run has emitted `loaded` but the preview is still
    being written; retry shortly;
  - `no_preview`: the run finished after `loaded` without a stored preview
    (building it failed); retrying will not help.

  As for every 4xx, `detail` is written for the user.
- `columns` are the **target contract columns** for the run's table, in
  contract order.
- `totalRows` is the number of rows in the uploaded file.
- `rows[].pk` is the row's primary key as a string. Composite keys join
  their parts with `|` in key order: for `lineitem`,
  `"l_orderkey|l_linenumber"` (e.g. `"1|3"`).
- `rows[].cells` is keyed by target column. `before` is the **raw source
  text** of the source column mapped to that target column (a renamed
  source column such as `amt` appears under `o_totalprice`); `null` when the
  source had no value or no such column. `after` is the canonical rendering
  of the loaded value (`null` when loaded as null). `flagged` marks a
  **cell-level** quarantine: a value the pipeline refused to guess, loaded
  as null and flagged for review.
- `rows[].quarantined` is `true` when the whole row was held back from the
  load; `reason` says why (`null` otherwise).

## Evals

`GET /evals/summary` returns the latest scored eval run.

- `split` is `dev` or `holdout`; `ood` (out-of-distribution) may be added
  later. `n` is the number of eval **cases**.
- Every `systems[]` metric is a 95% interval (`CI`) or `null` (not
  measured): `cellAccuracy`, `fabricationRate` (share of cells holding a
  value that is not supported by the source), `contractPassRate` (share of
  cases whose output passes every contract check), `passHat4` (estimated
  probability that all 4 runs of a case succeed, the τ-bench pass^k
  estimator), `p95LatencyMs` and `costPer1kRowsUsd`.
- `pareto[].points[].quality` is **cell accuracy** at that operating point,
  with `costPer1kRowsUsd` its cost. Baselines have one point and
  `lambda: null`; a router has one point per trade-off weight `lambda`.
- The `systems[]` row named `sluice` is the router's **default operating
  point**, `lambda = 0.5`: its `cellAccuracy.mean` and `costPer1kRowsUsd.mean`
  equal that Pareto point's `quality` and cost.
