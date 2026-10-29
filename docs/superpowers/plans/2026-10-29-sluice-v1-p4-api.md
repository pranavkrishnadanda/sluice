# Sluice v1 · Plan 4: API server, live runs in the UI, traces, demo GIF

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A FastAPI server that implements `docs/api/contract.md` over the plan-2 pipeline, so the existing Next.js UI runs real files live; OpenTelemetry traces in Phoenix; a GIF of the UI running a file end to end for the README.

**Architecture:** One API process (uvicorn, one worker, `127.0.0.1:8080`) holds the HTTP routes and an in-process job worker. `POST /runs` stores the upload in RustFS and inserts a `queued` row in a Postgres `api_runs` table; the worker claims rows with `SELECT … FOR UPDATE SKIP LOCKED` (spec "v1 job intake") and runs them through a shared execution core (also used by `sluice run`). The core relays the pipeline's own `EventLog` live: every event is written to a Postgres `run_events` table and published to an in-process hub, and the SSE route replays from the table then tails the hub. Preview, transforms and lineage come from per-run artefacts and the plan-2 lineage tables.

**Tech Stack:** Python 3.14, FastAPI, uvicorn, python-multipart, psycopg 3, pyarrow S3 filesystem (RustFS), OpenTelemetry SDK + OTLP/HTTP exporter, Arize Phoenix (container), Next.js UI (existing), Playwright, ffmpeg.

**Spec:** `docs/superpowers/specs/2026-10-28-sluice-design.md` (§7.11 API, §7.12 Web UI, observability "OTel → Phoenix", v1 deliverables 9–11, "v1 job intake"). **Contract (binding, field for field):** `docs/api/contract.md`; TypeScript source of truth `frontend/src/lib/api/types.ts`.

## Global Constraints

- Every request/response/event shape follows `docs/api/contract.md` exactly: camelCase JSON, ISO-8601 UTC timestamps, `{detail, code?}` error bodies on every 4xx/5xx (FastAPI's default 422 overridden), opaque ids `[A-Za-z0-9_-]{1,64}`.
- API binds `127.0.0.1:8080` (port 8000 is reserved on the owner's machine). Never touch 5432, 8000, 3000; 3100 is the controller's UI preview.
- One uvicorn worker process (the event hub and job worker are in-process). Job concurrency 1 by default (`SLUICE_API_CONCURRENCY`, max 4) because of Groq free-tier limits (30 RPM / 8K TPM per model).
- No auth, no cookies in v1; CORS allow-list from `SLUICE_UI_ORIGINS` (default `http://localhost:3000,http://127.0.0.1:3000,http://localhost:3100,http://127.0.0.1:3100`), applied to every route incl. SSE and errors; preflight allows `Last-Event-ID`.
- Uploads: `.csv`, `.json`, `.parquet` only (415 otherwise); 100 MB (413), enforced while streaming, never by buffering the whole body; stored under a generated key, never under the client's file name.
- Secrets only via `backend/.env` / `Settings` (`SecretStr`); never in events, logs, spans, error bodies or artefacts. Never read, print or commit `.env`.
- Phoenix runs only under the compose profile `observability` (not started by `make up`); tracing is a no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Host ports: Phoenix UI `56006`, OTLP/HTTP `54318`, both bound to `127.0.0.1`.
- Ruling P2 (carried): Task 1 adds every new dependency (latest stable, verified on PyPI before pinning); later tasks must not edit `pyproject.toml`/`uv.lock`/`bun.lock`. Owner's uv 0.10.2 — don't upgrade. Bun for frontend packages.
- `sluice run` keeps its current observable behaviour (same events, exit codes and load semantics, Rulings F1/L1/C1–C4c) after the execution core is shared.
- The rules baseline in `evals/RESULTS.md` stays reproducible byte-for-byte (CI "Reproduce published results").
- Ruff, pytest, line length 100; Biome/Oxlint/tsgo for frontend; Conventional Commits; the owner is the sole author — no `Co-Authored-By`, no AI attribution. TDD: failing test first.

## Review Focus

1. A browser tab closed mid-stream, then reopened with `Last-Event-ID` → the server stops tailing for the dead client (no leaked task), and the new stream resumes at `seq+1` with no gap and no duplicate. (Task 6)
2. The API process restarts while a run is `running` → on startup that run gets exactly one `run_finished(failed, "the server restarted during the run")` and status `failed`; `queued` runs are picked up again. (Task 4)
3. A >100 MB upload sent chunked (no `Content-Length`) → `413` after reading at most limit + one chunk; nothing stored, no row inserted. (Task 5)
4. The same file uploaded twice at once for the same table → both runs finish; one loads, the other ends `succeeded` with "file already loaded" and loads nothing (Ruling L1), never duplicate rows. (Task 4)
5. A file name like `../../etc/passwd.csv` or one with control characters → stored under a generated key; `fileName` echoed back as the sanitised basename, ≤ 255 chars. (Task 5)

---

## File Structure

```
backend/src/sluice/
  runs/
    __init__.py
    execute.py        # Task 3: execute_run — shared by `sluice run` and the API worker
    preview.py        # Task 3: build_preview / page_preview (pure)
  api/
    __init__.py
    app.py            # Task 1: create_app(), error handlers, CORS, lifespan
    settings.py       # Task 1: ApiSettings
    schemas.py        # Task 1: contract response models
    store.py          # Task 2: Postgres RunStore (api_runs, run_events, run_artefacts)
    blobs.py          # Task 2: BlobStore protocol; S3BlobStore (RustFS), LocalBlobStore
    hub.py            # Task 4: EventHub (in-process publish/subscribe per run)
    worker.py         # Task 4: Worker (claim → execute_run → persist), startup recovery
    routes_runs.py    # Task 5: POST/GET /runs, transforms, preview
    routes_evals.py   # Task 5: GET /evals/summary
    routes_events.py  # Task 6: GET /runs/{id}/events (SSE)
  tracing.py          # Task 7: setup_tracing(), span helpers (GenAI semconv)
frontend/e2e/live-backend.spec.ts   # Task 8
frontend/scripts/record-demo.ts     # Task 9
compose.yaml, Makefile, README.md   # Tasks 1, 7, 8, 9
```

---

### Task 1: Dependencies, settings, app skeleton, error shape, CORS

**Files:**
- Modify: `backend/pyproject.toml`, `backend/uv.lock`, `Makefile` (`api: cd backend && PYTHONPATH=src uv run uvicorn sluice.api.app:create_app --factory --host 127.0.0.1 --port 8080 --workers 1`)
- Create: `backend/src/sluice/api/{__init__,app,settings,schemas}.py`, `backend/tests/api/test_app.py`, `backend/tests/api/test_schemas.py`

**Interfaces:**
- Produces: `create_app(*, settings: ApiSettings | None = None, store: RunStore | None = None, blobs: BlobStore | None = None, worker: bool = True) -> FastAPI`. Later tasks register their routers in it; tests pass fakes and `worker=False`.
- `ApiSettings` (pydantic-settings, `env_prefix="SLUICE_API_"`): `ui_origins: list[str]` (env `SLUICE_UI_ORIGINS`, comma-separated, default per Global Constraints), `max_upload_bytes: int = 100 * 1024 * 1024`, `concurrency: int = Field(1, ge=1, le=4)`, `keepalive_s: float = 15.0`, `landing_bucket: str = "sluice-landing"`.
- `schemas.py`: Pydantic models mirroring `types.ts` (`RunSummary`, `Transform`, `PreviewCell`, `PreviewRow`, `DataPreview`, `CI`, `EvalSummary` with nested system and pareto models), base config `alias_generator=to_camel, populate_by_name=True`, dumped `by_alias=True`. Run events reuse `sluice.agent.events` models (already contract-shaped).
- `ApiError(status: int, detail: str, code: str | None = None)` raised by routes.

- [ ] **Step 1: Add deps** — latest stable, verified on PyPI first: `fastapi`, `uvicorn[standard]`, `python-multipart`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`. `uv lock` with uv 0.10.2. Record versions in the task report.
- [ ] **Step 2: Failing tests** (`TestClient`, fakes):
  - `GET /healthz` → 200 `{"status":"ok"}`.
  - Unknown route → 404 body exactly `{"detail": <str>, "code": "not_found"}`.
  - A request-validation error → 422 `{"detail": <str>, "code": "invalid_request"}` (not FastAPI's list).
  - An unhandled exception in a route → 500 `{"detail": "Internal error.", "code": "internal"}`, no traceback or secret in the body.
  - `OPTIONS /runs` from `Origin: http://localhost:3100` requesting header `last-event-id` → allowed; that origin gets `Access-Control-Allow-Origin` on a 404 and on a 500; `Origin: http://evil.test` gets none.
  - `test_schemas.py`: each model dumps exactly the key set of `types.ts`, e.g. `RunSummary` → `{"id","fileName","table","rows","status","createdAt","costUsd"}`.
- [ ] **Step 3: Implement** `create_app`, exception handlers (`HTTPException`, `RequestValidationError`, `ApiError`, catch-all), `CORSMiddleware` with `allow_origins=settings.ui_origins`, `allow_methods=["GET","POST","OPTIONS"]`, `allow_headers=["Last-Event-ID","Content-Type"]`, no credentials. The catch-all 500 must still carry CORS headers (verify with the test).
- [ ] **Step 4: PASS**, `make lint && make test`.
- [ ] **Step 5: Commit** `feat(api): add FastAPI app skeleton with contract errors and CORS`

---

### Task 2: Run store (Postgres) and blob store (RustFS)

**Files:**
- Create: `backend/src/sluice/api/store.py`, `backend/src/sluice/api/blobs.py`, `backend/tests/api/test_store.py`, `backend/tests/integration/test_api_store.py` (`-m integration`)

**Interfaces:**
- DDL (idempotent, `RunStore.ensure_schema()`, under a Postgres advisory lock like `load/iceberg.py`):
  - `api_runs(id text PK, file_name text NOT NULL, table_name text NOT NULL, blob_key text NOT NULL, file_sha256 text NOT NULL, rows integer NULL, status text NOT NULL CHECK (status IN ('queued','running','succeeded','quarantined','failed')), cost_usd double precision NOT NULL DEFAULT 0, created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz)`; index `(status, created_at)`.
  - `run_events(run_id text REFERENCES api_runs(id), seq integer NOT NULL CHECK (seq >= 1), at timestamptz NOT NULL, type text NOT NULL, payload jsonb NOT NULL, PRIMARY KEY (run_id, seq))`.
  - `run_artefacts(run_id text PK REFERENCES api_runs(id), preview_key text, transforms jsonb NOT NULL DEFAULT '[]')`.
- `RunStore(dsn: str)` (short transactions; `SET LOCAL lock_timeout`/`statement_timeout` as in `load/iceberg.py`):
  - `create_run(*, run_id, file_name, table, blob_key, file_sha256) -> RunSummary` (status `queued`)
  - `list_runs(limit: int = 100) -> list[RunSummary]` (newest first)
  - `get_run(run_id) -> RunRow | None` (`RunRow` = summary + `blob_key`, `file_sha256`, timestamps)
  - `claim_next() -> RunRow | None` — `UPDATE api_runs SET status='running', started_at=now() WHERE id = (SELECT id FROM api_runs WHERE status='queued' ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING …`
  - `append_event(run_id, event: EventModel) -> None` (raises on a duplicate seq)
  - `events_after(run_id, after_seq: int) -> list[EventModel]` (ordered; parsed with `PIPELINE_EVENT_ADAPTER`)
  - `update_progress(run_id, *, rows: int | None = None, cost_usd: float | None = None) -> None`
  - `finish(run_id, status, cost_usd) -> None` (terminal statuses only)
  - `save_artefacts(run_id, *, preview_key: str | None, transforms: list[dict]) -> None`; `get_artefacts(run_id) -> Artefacts | None`
  - `interrupted() -> list[RunRow]` (status `running`, for startup recovery)
- `BlobStore` protocol: `async put_stream(key: str, chunks: AsyncIterator[bytes], *, max_bytes: int) -> int` (raises `TooLarge` once more than `max_bytes` arrive and removes the partial object), `open_local(key) -> ContextManager[Path]` (temporary local copy, removed on exit), `put_bytes(key, data)`, `get_bytes(key) -> bytes`, `delete(key)`. `S3BlobStore` (pyarrow `S3FileSystem` against RustFS using the endpoint/keys in `Settings`; bucket created if missing) and `LocalBlobStore(root: Path)`.

- [ ] **Step 1: Failing tests:** `claim_next` returns the oldest queued run once, and two concurrent claimers never get the same row; `events_after(run, 3)` returns seq 4.. in order; duplicate seq raises; `finish` rejects `queued`/`running`; `LocalBlobStore.put_stream` over the limit raises `TooLarge` and leaves no file; integration: the same on real Postgres + RustFS (a 3 MB stream in 64 KB chunks round-trips; over-limit leaves no object).
- [ ] **Step 2–4:** FAIL → implement → PASS (`make test`; `make up && make test-integration`).
- [ ] **Step 5: Commit** `feat(api): add Postgres run store and RustFS blob store`

---

### Task 3: Shared execution core with live events; preview artefact; transform ids in lineage

**Files:**
- Create: `backend/src/sluice/runs/{__init__,execute,preview}.py`, `backend/tests/runs/test_execute.py`, `backend/tests/runs/test_preview.py`
- Modify: `backend/src/sluice/cli.py` (`run_file` becomes a thin wrapper over `execute_run`), `backend/src/sluice/agent/events.py` (`EventLog(…, on_emit: Callable[[EventModel], None] | None = None)`, called after stamping, outside the lock), `backend/src/sluice/agent/pipeline.py` (caller-supplied `run_id`; `meta["mapping"]: dict[str, str | None]`, contract column → source column), `backend/src/sluice/systems/agent.py` (pass `run_id` and the live log through), `backend/src/sluice/load/iceberg.py` (`TransformRecord.transform_id: OpaqueId`; `transforms.transform_id text` via idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS`)

**Interfaces:**
- `execute_run(path: Path, table: str, *, run_id: str, emit: Callable[[EventModel], None], contract_version: str = "v1", run_fn: RunFn | None = None, load_fn=…, loaded_fn=…, settings_fn=…, reference_fn=…) -> RunOutcome`, with `RunOutcome(result: LoadResult | None, status: Literal["succeeded","quarantined","failed"], output: SystemOutput | None, cost_usd: float, tokens: int)`.
  - `emit` receives events **live, in order, stamped by one public `EventLog`**: `run_started`, the pipeline's own events as they happen (minus its `run_started`/`run_finished`), then `loaded` if written, then exactly one `run_finished`.
  - `run_started.rows` is the row count when the file can be read first (read it once up front); `null` only if reading fails.
  - Semantics otherwise identical to today's `run_file` (Rulings F1, L1, C1–C4c). `run_file(...)` = `execute_run(..., emit=lambda e: echo(e.to_json()))` and raises the same exceptions.
- The live relay: the agent pipeline's `EventLog(on_emit=…)` forwards each pipeline event into the public log (renumbered `seq`, same payload). Systems without a live log (rules-only) are replayed from `meta["events"]` as today.
- One `run_id` end to end: public run id = pipeline `run_id` = lineage `run_id` = input to `transform_id(...)`, so every `node_finished.transformId` equals a persisted transform id (closes the plan-2 deferral).
- `build_preview(raw: pl.DataFrame, out: SystemOutput, contract: Contract) -> pl.DataFrame`: one row per raw row with its PK text, per contract column `before`/`after`/`flagged`, and `quarantined`/`reason`. `before` = raw text of the source column in `out.meta["mapping"]` (null if unmapped or null); `after` = canonical rendering of the loaded value (decimal `"124500.50"`, ISO date, ISO-8601 UTC timestamp ending `Z`), null if loaded as null; `flagged` from `out.flags`; held-back rows `quarantined=True`, `reason=meta["quarantine_reasons"][pk]`. Stored as Parquet.
- `page_preview(preview: pl.DataFrame, contract: Contract, *, offset: int, limit: int) -> DataPreview` (`limit` capped at 1,000; `nextOffset` null after the last page; composite PK joined with `|` in key order; `totalRows` = rows in the uploaded file).

- [ ] **Step 1: Failing tests:**
  - `execute_run` with a scripted pipeline (FunctionModel + fake sandbox, as in `tests/agent/test_pipeline.py`) and a recording `emit`: `node_started` is emitted before the fake sandbox returns (live, not replayed); `seq` 1..n with no gaps; exactly one `run_started` and one `run_finished`; `loaded` only when written.
  - CLI regression: the existing `tests/test_cli_run.py` passes **unchanged**.
  - Every `node_finished.transformId` is in `meta["transforms"]` and, in the integration test (`tests/integration/test_load.py`), in the `transforms` table.
  - `build_preview` on a renamed `amt` column: `before == "1,24,500.50"` under `o_totalprice`, `after == "124500.50"`; a destroyed `"N/A"` → `after is None and flagged`; an FK-quarantined row → `quarantined` with its reason; `page_preview(offset=0, limit=2)` on 5 rows → 2 rows, `nextOffset == 2`; `limit=5000` → 1,000; lineitem PK `"1|3"`.
- [ ] **Step 2–4:** FAIL → implement → PASS; rules baseline `RESULTS.md` byte-identical; `make lint && make test`.
- [ ] **Step 5: Commits** `refactor: share run execution between the CLI and the API with live events`, `feat: build the before/after preview and persist transform ids`

---

### Task 4: Event hub and job worker

**Files:**
- Create: `backend/src/sluice/api/hub.py`, `backend/src/sluice/api/worker.py`, `backend/tests/api/test_hub.py`, `backend/tests/api/test_worker.py`, `backend/tests/integration/test_api_worker.py`
- Modify: `backend/src/sluice/api/app.py` (lifespan starts/stops the worker, runs `recover()` first)

**Interfaces:**
- `EventHub`: `publish(run_id, event) -> None` (thread-safe; from the worker thread via `loop.call_soon_threadsafe`), `subscribe(run_id)` (async context manager yielding an async iterator; bounded queue of 1,000 — an overflowing subscriber is closed and must reconnect, replaying from the store), `subscriber_count(run_id) -> int`, `close(run_id)` after `run_finished`.
- `Worker(store, blobs, hub, *, concurrency: int, execute=execute_run, settings_fn=get_settings)`: `async start()`, `async stop()`, `notify()`.
  - Loop: `claim_next()` → `blobs.open_local(blob_key)` → `execute_run` in `asyncio.to_thread`, with `emit` = `store.append_event` **then** `hub.publish` (the store first, so a replay never misses an event the hub delivered).
  - Progress: `rows` from `run_started`; `cost_usd` running total from each `attempt_failed`/`node_finished`, set to `run_finished.costUsd` at the end (contract "Run summaries").
  - After the run: `store.finish`; if the output exists, `build_preview` → `blobs.put_bytes(f"runs/{id}/preview.parquet")`; `save_artefacts` with `meta["transforms"]` (+ `transform_id`, plan order).
  - Idle: polls `claim_next` every 0.5 s; `notify()` wakes it immediately.
- `Worker.recover()`: for each `store.interrupted()` run, append `run_finished(status="failed", summary="the server restarted during the run", costUsd=<stored>, tokens=0)` with the next seq, then `finish(…, "failed", …)`.

- [ ] **Step 1: Failing tests** (fake store/blobs/execute): a queued run's events land in the store in seq order and reach a subscriber; `cost_usd` grows with node events and equals `run_finished.costUsd`; an `execute` that raises still yields exactly one `run_finished(failed)`; two queued runs with concurrency 1 run one after the other; `recover()` closes an interrupted run with exactly one `run_finished(failed)` (Review Focus 2); integration: two uploads of the same file to `orders` → one loads, the other ends `succeeded` with "file already loaded", and the Iceberg table holds one file's rows (Review Focus 4).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(api): add the job worker and in-process event hub`

---

### Task 5: Run routes, preview, transforms, eval summary

**Files:**
- Create: `backend/src/sluice/api/routes_runs.py`, `backend/src/sluice/api/routes_evals.py`, `backend/tests/api/test_routes_runs.py`, `backend/tests/api/test_routes_evals.py`

**Interfaces:**
- `POST /runs` (multipart `file`, `table`):
  - `table` not in {orders, lineitem} → 422 `code="unknown_table"`; missing field → 422 `invalid_request`.
  - Extension not in {.csv, .json, .parquet} (case-insensitive) → 415 `code="unsupported_format"`.
  - Stream into `blobs.put_stream(f"uploads/{run_id}/input{ext}", …, max_bytes=settings.max_upload_bytes)`; `TooLarge` → 413 `code="too_large"`, `detail="File too large: the limit is 100 MB."`. Read the multipart body incrementally (no `await file.read()` of the whole body).
  - Parse-check with `read_input` on the local copy; failure → 422 `code="unparseable"` and the blob is deleted.
  - `file_sha256`; `store.create_run`; `worker.notify()`; 201 `RunSummary`.
  - `fileName` = sanitised basename: strip path components and control characters, NFC-normalise, ≤ 255 chars; empty → `"upload<ext>"`.
- `GET /runs` → `store.list_runs(100)`.
- `GET /runs/{id}/transforms` → 404 unknown; else `artefacts.transforms` as `Transform` (`language="polars"`), plan order then attempt; `[]` while there are none.
- `GET /runs/{id}/preview?offset&limit` → 404 unknown; 409 `code="not_loaded"` until the run's events contain `loaded`; else `page_preview` of the stored Parquet; `offset < 0` or `limit < 1` → 422.
- `GET /evals/summary`, built from `latest_summaries` over `evals/runs` + `evals/published`:
  - Take the newest `agent` summary; `split`, `n`, `generatedAt` from it; `systems` = every summary with the same `(split, limit)`; `agent` is named `"sluice"`, others keep their system name.
  - `cellAccuracy`/`fabricationRate`/`contractPassRate` from the summary CIs; `passHat4` from `pass_hat_k` when trials == 4, else `null`; `p95LatencyMs` = 95th-percentile per-case latency × 1000 with a bootstrap CI (existing `evals.stats`) when `results.jsonl` is present, else `null`; `costPer1kRowsUsd` `null` (rows per case aren't recorded yet); `pareto: []` until plan 3.
  - No agent summary → 404 `code="no_evals"`.

- [ ] **Step 1: Failing tests** (TestClient, fakes): each status/code above; a chunked 101 MB body without `Content-Length` → 413 after pulling ≤ limit + one chunk from the fake stream, and no row inserted (Review Focus 3); `../../etc/passwd.csv` → `fileName == "passwd.csv"`, blob key under `uploads/<run_id>/` (Review Focus 5); preview 409 before `loaded`, then 200 with the contract columns; the committed `evals/published` fixtures map to `systems[0].system == "sluice"` with `cellAccuracy.mean ≈ 0.9667`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(api): add run, transform, preview and eval routes`

---

### Task 6: SSE event stream

**Files:**
- Create: `backend/src/sluice/api/routes_events.py`, `backend/tests/api/test_routes_events.py`

**Interfaces:**
- `GET /runs/{id}/events` → 404 unknown; else `StreamingResponse(media_type="text/event-stream")` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`.
- Resume point = `int(Last-Event-ID)` when it is a non-negative integer, else 0.
- Algorithm: subscribe to the hub **first**, then replay `store.events_after(id, resume)`, then forward live events with `seq > last_sent` (drops the subscribe/replay overlap). Message format `id: <seq>\ndata: <event JSON, camelCase, one line>\n\n`. Send `: keep-alive\n\n` whenever nothing was sent for `settings.keepalive_s`. Close after sending `run_finished`; a finished run replays and closes. On client disconnect release the subscription.

- [ ] **Step 1: Failing tests** (httpx `AsyncClient` over ASGI, fake store/hub, `keepalive_s=0.05`): a finished run replays ending with `run_finished` and the stream closes; an event published between subscribe and replay appears exactly once; `Last-Event-ID: 3` → first message `id: 4`; a queued run gets `: keep-alive` lines before its first event; a disconnect mid-stream brings `hub.subscriber_count` back to 0 (Review Focus 1); unknown run → 404 JSON; every `data:` line parses as a contract `RunEvent`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(api): stream run events over SSE with replay and resume`

---

### Task 7: OpenTelemetry traces → Phoenix

**Files:**
- Create: `backend/src/sluice/tracing.py`, `backend/tests/test_tracing.py`
- Modify: `compose.yaml` (service `phoenix`: `arizephoenix/phoenix` pinned to the latest stable tag and digest, `profiles: ["observability"]`, UI `127.0.0.1:56006` → container UI port, the documented OTLP/HTTP port → `127.0.0.1:54318`; verify both container ports in Phoenix's docs), `Makefile` (`up-obs: $(COMPOSE) --profile observability up -d --wait`), `backend/.env.example` (commented `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:54318`, `OTEL_SERVICE_NAME=sluice`), `backend/src/sluice/llm/client.py`, `backend/src/sluice/agent/pipeline.py`, `backend/src/sluice/sandbox/runner.py`, `backend/src/sluice/runs/execute.py`, `backend/src/sluice/api/app.py`, `backend/src/sluice/cli.py`

**Interfaces:**
- `setup_tracing(service_name: str = "sluice") -> None` — idempotent; installs a `TracerProvider` with `BatchSpanProcessor(OTLPSpanExporter())` only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, otherwise leaves the no-op provider. Called by `create_app` and the CLI entry point.
- Spans (names fixed): `sluice.run` (`sluice.run_id`, `sluice.table`, `sluice.status`); `sluice.step` per step (`sluice.step` ∈ `profile|rules_pass_1|mapping|plan|rules_pass_2|load`); `sluice.node.attempt` (`sluice.node_id`, `sluice.attempt`, `sluice.tier`, `sluice.outcome`); `sluice.sandbox` (`sluice.exit_code`, `sluice.timed_out`, `sluice.duration_ms`); one `chat <tier>` per LLM call with GenAI semantic-convention attributes `gen_ai.operation.name="chat"`, `gen_ai.system="litellm"`, `gen_ai.request.model=<tier>`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, plus `sluice.role`, `sluice.cost_usd`. No prompt or completion text and no secrets in attributes.

- [ ] **Step 1: Failing tests** with `InMemorySpanExporter`: a scripted pipeline run gives one `sluice.run` root with the step spans as descendants, one `chat <tier>` span per `CallRecord` with matching token attributes, a `sluice.sandbox` span per attempt; no attribute value contains the fake API key or prompt text; with the env var unset `setup_tracing` installs nothing.
- [ ] **Step 2–4:** FAIL → implement → PASS; manual: `make up-obs`, endpoint set in `.env` by the owner, one `sluice run`, trace visible at `http://127.0.0.1:56006` (screenshot path in the report).
- [ ] **Step 5: Commit** `feat: trace runs, LLM calls and sandbox runs with OpenTelemetry`

---

### Task 8: UI on the real backend

**Files:**
- Create: `frontend/e2e/live-backend.spec.ts` (skipped unless `SLUICE_E2E_API_URL` is set)
- Modify: `frontend/README.md`, root `README.md` (Getting started: `make up && make api`, `NEXT_PUBLIC_SLUICE_API_URL=http://localhost:8080` + rebuild), `frontend/src/**` only for contract-conformance bugs the e2e finds (each listed in the report)

**Interfaces:** consumes the running API only.

- [ ] **Step 1: Failing e2e** against `make up && make api` with the owner's keys: upload an existing dev case input (`backend/cases/dev/<id>/input.*`) for its table; the run appears in the sidebar; steps stream in live (`plan_created` visible before `run_finished`); a final status badge; the Data tab shows before/after rows; the Code tab shows the accepted transform when the plan had nodes; no "Demo data" badge; no console errors; axe passes as in the existing e2e.
- [ ] **Step 2–4:** FAIL → fix → PASS. Serve the UI on a port the controller assigns (never 3000; 3100 only after the controller stops its preview).
- [ ] **Step 5: Commit** `test(e2e): run a real file through the live backend` (+ `fix(frontend): …` per conformance fix)

---

### Task 9: Demo GIF and README (owner-assisted)

**Files:**
- Create: `frontend/scripts/record-demo.ts` (Playwright, 1440×900, `recordVideo`: upload a hard dev case, wait for `run_finished`, open the Plan, Code and Data tabs, toggle dark/light once), `docs/media/sluice-demo.gif`
- Modify: `Makefile` (`demo-gif`: record, then `ffmpeg -i <video>.webm -vf "fps=12,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" docs/media/sluice-demo.gif`), `README.md` (GIF under the title; Web UI section; roadmap marks Plan 4 done; status line)

- [ ] **Step 1:** With the stack up and the UI on the real backend, `make demo-gif`; GIF ≤ 8 MB and ≤ 30 s; nothing secret or personal on screen.
- [ ] **Step 2:** README updated as above; links to this plan and `docs/api/contract.md`.
- [ ] **Step 3: Commit** `docs: add the end-to-end demo GIF`

---

## Execution waves

- **A:** Task 1.
- **B (parallel):** Task 2, Task 3, Task 7. Task 7 also edits `pipeline.py`: its edits there are span wrappers only, and it merges after Task 3.
- **C:** Task 4 (needs 2, 3).
- **D (parallel):** Task 5, Task 6 (need 4).
- **E:** Task 8, then Task 9.

## Non-goals (v1)

- Follow-up questions in the composer (spec §7.12) and the Lineage/Cost tabs (spec v2 list): not in the API contract yet.
- Pareto/router points in `/evals/summary` (plan 3).
- Auth, multiple API processes, Redpanda intake (v2).

## Done when

- `make test` passes with no services or keys; `make up && make test-integration` passes; the API serves every contract route; the UI built with `NEXT_PUBLIC_SLUICE_API_URL=http://localhost:8080` runs a real file live end to end with no "Demo data" badge.
- `sluice run` behaves exactly as before; `RESULTS.md` reproduces.
- A run's trace is visible in Phoenix with LLM spans carrying GenAI attributes.
- The README shows the demo GIF.
