# Sluice v1 · Plan 2 — Agent pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the LLM agent system to Sluice — schema mapping, a self-written repair plan, worker subagents whose transforms run in a locked-down sandbox, an independent verifier and a bounded repair loop — register it as eval system `agent`, load accepted output into Iceberg with lineage, and publish its first measured numbers next to the rules baseline.

**Architecture:** The agent never replaces the deterministic rules; it *rewrites raw input text so the rules can parse it*. Pipeline per file: read → **map** (LLM renames source columns to contract columns) → rules pass 1 → **plan** (LLM picks the flagged columns that look recoverable) → per node a **worker** writes `transform(values: pl.Series) -> pl.Series` (String → String), run in a **sandbox** container → rules pass 2 on the rewritten raw frame → code guards + **verifier** (LLM) → repair (≤ 3 attempts, escalating tier) → final `SystemOutput`. All LLM calls go through a LiteLLM proxy (fallbacks, retries, rate limits, Redis cache, cost); Pydantic AI gives typed outputs.

**Tech Stack:** Python 3.14 (uv), Pydantic AI, LiteLLM proxy (Docker), Redis, Postgres, RustFS (S3 API), PyIceberg (SQL catalog in Postgres), Docker SDK (sandbox on Colima), Polars, pydantic-settings, respx; models: Groq (strong + small fallback), Gemini via Google AI Studio (strong fallback), local vLLM via vllm-metal (small, optional).

**Spec:** `docs/superpowers/specs/2026-10-28-sluice-design.md` (§7.3, §7.4, §7.5, §7.6, §9 v1 items 1, 4, 5; §11 items 1–7). Plan 1 code is on `main` (`backend/`). The UI branch amends `docs/api/contract.md`; this plan's event model must match it after that branch merges (Task 3).

## Global Constraints

- Python deps via **uv** only; latest stable, verified at the task that adds them (seen 2026-10-28: litellm 1.106.5, pydantic-ai 2.63.0, pyiceberg 0.12.0, docker 7.2.0, redis 8.1.0, psycopg 3.3.6, pydantic-settings 2.16.2, respx 0.23.1). Images (verify tags): `postgres:18.6`, `redis:8.8.4`, `rustfs/rustfs:1.0.4`, LiteLLM proxy `ghcr.io/berriai/litellm` at the tag matching the litellm release.
- Host ports (never 5432, 8000, 3000, 3100): Postgres **55432**, Redis **56379**, RustFS API **59000** / console **59001**, LiteLLM **54000**, local vLLM **58000**; plan 4's API will use **8080**.
- Secrets only in `backend/.env` (git-ignored), loaded via pydantic-settings and Compose `env_file`; commit `backend/.env.example` with names only: `GROQ_API_KEY`, `GEMINI_API_KEY`, `LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `RUSTFS_ACCESS_KEY`, `RUSTFS_SECRET_KEY`. **Never read, print, log or commit real values**; settings `repr` must mask secrets.
- Unit tests never call real LLMs or need network/Docker: use Pydantic AI `FunctionModel`/`TestModel` and fakes. Tests needing services are marked `integration` (Docker) or `live` (real API keys) and are excluded from `make test` and CI by default.
- **No metric depends on human labels.** The agent must never fabricate: any cell whose raw text is a destroyed token (`"N/A"`, `""`, `"-1"`, `"###"`) or whose information is gone must end up null + flagged.
- Generated transforms run only inside the sandbox — never `exec`/`eval` in the host process, including in tests.
- Caps (constants in `sluice/agent/caps.py`): `MAX_ATTEMPTS = 3` per node, `MAX_TOKENS_PER_FILE = 60_000`, `MAX_WALL_S_PER_FILE = 180`.
- Commits: Conventional Commits; owner is sole author — **no `Co-Authored-By` or any AI attribution**. CLI runs as `cd backend && PYTHONPATH=src uv run sluice …` (Ruling J); pytest via `uv run python -m pytest`.

## Review Focus

1. **Model returns malformed or hallucinated output** (mapping to a column not in the contract, two sources mapped to one target, plan naming an unflagged column, code without `transform`) → rejected by validation, retried once with the error, then the step is skipped (rules-only result stands); never a crash. Pinned in Tasks 5, 6, 7.
2. **Provider down / rate-limited / keys missing** → LiteLLM falls back (Groq → Gemini; vLLM → Groq small); if every route fails, the file finishes as the rules-only output with `meta["llm_error"]`, not an exception. Pinned in Tasks 2 and 8.
3. **A transform that fills a destroyed token** (e.g. `"N/A"` → `"0.00"`) → the fabrication guard rejects the attempt before rules pass 2. Pinned in Task 7.
4. **Sandbox abuse** (network, writes outside scratch, reading host env, fork bomb, memory blow-up, infinite loop) → contained; the attempt fails with a reason. Pinned in Task 4.
5. **Same file twice** → cache hits (no new spend) and an idempotent Iceberg load (no duplicate rows). Pinned in Tasks 2 and 9.

---

## File Structure

```
compose.yaml                         # postgres, redis, rustfs, litellm
backend/.env.example
backend/litellm/config.yaml          # model groups strong/small, fallbacks, retries, rpm/tpm, cache
backend/sandbox/Dockerfile           # runner image: python + polars only
backend/sandbox/run_transform.py     # in-container entrypoint
backend/src/sluice/
  settings.py                        # Settings (pydantic-settings), masked secrets
  llm/client.py                      # LLM facade over Pydantic AI → LiteLLM; CallRecord
  llm/prices.py                      # dated price table, cost from tokens
  sandbox/runner.py                  # SandboxResult, run_transform()
  agent/caps.py                      # constants
  agent/events.py                    # PipelineEvent models (mirror docs/api/contract.md RunEvent)
  agent/mapping.py                   # ColumnMapping, propose_mapping(), apply_mapping()
  agent/plan.py                      # PlanNode, Plan, candidate_columns(), propose_plan(), route_tier()
  agent/worker.py                    # write_transform(), guard_output()
  agent/verifier.py                  # Verdict, verify_node()
  agent/pipeline.py                  # run_pipeline() state machine
  systems/agent.py                   # AgentSystem (name "agent")
  load/iceberg.py                    # load_output(), lineage
backend/tests/{llm,sandbox,agent,integration}/...
```

---

### Task 1: Services stack and settings

**Files:**
- Create: `compose.yaml`, `backend/.env.example`, `backend/src/sluice/settings.py`, `backend/tests/test_settings.py`, `backend/tests/integration/test_services.py`
- Modify: `Makefile` (`up`, `down`, `ps`, `test-integration`), `backend/pyproject.toml` (`uv add pydantic-settings "psycopg[binary]" redis`; markers `integration`, `live`; default selection excludes `integration`, `live`, `slow`), `.gitignore` (`backend/.env`)

**Interfaces:**
- Produces: `Settings` (pydantic-settings, `env_file="backend/.env"`) with `groq_api_key: SecretStr | None`, `gemini_api_key: SecretStr | None`, `litellm_master_key: SecretStr`, `litellm_url: str = "http://localhost:54000"`, `postgres_password: SecretStr`, `postgres_dsn` (computed: `postgresql://sluice:<password>@localhost:55432/sluice`, never printed), `redis_url: str = "redis://localhost:56379/0"`, `s3_endpoint: str = "http://localhost:59000"`, `rustfs_access_key: SecretStr`, `rustfs_secret_key: SecretStr`, `warehouse_bucket: str = "sluice-warehouse"`; `get_settings() -> Settings` (cached).
- `compose.yaml`: `postgres` (55432), `redis` (56379), `rustfs` (59000/59001, named volume; bucket `sluice-warehouse` created at startup), `litellm` (54000; in profile `llm` until Task 2). Healthchecks on all; `env_file: backend/.env`; mem limits postgres 512m, redis 128m, rustfs 512m, litellm 768m.

- [ ] **Step 1: Write failing tests:** `test_settings_masks_secrets` (repr/str never contain secret values); `test_settings_reads_env_file(tmp_path)` (fields populated; DSN contains port 55432); `test_settings_missing_llm_keys_ok` (no GROQ/GEMINI → None, no exception). Integration: `test_postgres_up`, `test_redis_up`, `test_rustfs_bucket_exists`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement; `make up` = `docker compose --env-file backend/.env up -d --wait`.
- [ ] **Step 4:** `make test` PASS. Owner check (in report): `cp backend/.env.example backend/.env`, fill values, `make up`, `make test-integration` PASS.
- [ ] **Step 5: Commit** `feat: add services stack and settings`

---

### Task 2: LLM layer — LiteLLM config, client facade, cost

**Files:**
- Create: `backend/litellm/config.yaml`, `backend/src/sluice/llm/client.py`, `backend/src/sluice/llm/prices.py`, `backend/tests/llm/test_client.py`, `backend/tests/integration/test_litellm_live.py` (marked `live`)
- Modify: `compose.yaml` (`litellm` out of the `llm` profile), `backend/pyproject.toml` (`uv add pydantic-ai`, `uv add --dev respx`)

**Interfaces:**
- `config.yaml` model groups: **`strong`** — primary a Groq 70B-class or Qwen3-32B model (pick from Groq's current list at setup; record it), fallback a Gemini Flash-class model via AI Studio; **`small`** — primary `openai/<worker model>` at `http://host.docker.internal:58000/v1` (local vLLM), fallback a small Groq model. `router_settings`: `num_retries: 2` only on 429/5xx/timeout, `timeout: 60`, fallbacks as above; per-deployment `rpm`/`tpm` at the providers' real free-tier limits (read from the Groq console at setup; record) with Redis-backed usage tracking — this replaces the spec's hand-written token bucket (**Ruling P1**); `cache: true` on Redis (TTL 30 days); master key from env.
- `CallRecord(role: str, tier: Literal["small","strong"], model: str, input_tokens: int, output_tokens: int, cost_usd: float, latency_ms: int, cached: bool)`.
- `LLM(base_url: str, api_key: str, *, model_override: Model | None = None)` with `async run(self, role: str, tier: Tier, system: str, prompt: str, output_type: type[T]) -> tuple[T, CallRecord]` — a Pydantic AI `Agent` on an OpenAI-compatible model pointed at LiteLLM (`model=<tier>`), `output_type`, one schema-repair retry; raises `LLMOutputError` after it, `LLMUnavailable` when every route fails. `model_override` injects `FunctionModel`/`TestModel`. `records: list[CallRecord]`.
- `prices.py`: `PRICE_TABLE_DATE`, `PRICES: dict[str, tuple[float, float]]` (USD per 1M in/out tokens at list price; free-tier models priced at list per spec §7.4); `cost_usd(model, in_tok, out_tok) -> float`. Prefer LiteLLM's response-cost header when present.

- [ ] **Step 1: Write failing tests:** typed output + record (FunctionModel); invalid output twice → `LLMOutputError`; cost from table exact; respx-mocked proxy response with cache-hit header → `cached=True`, cost 0; proxy 503 on all routes → `LLMUnavailable`; no API key text in records or error messages. Live: one `strong` and one `small` call through the proxy return valid objects.
- [ ] **Step 2–4:** FAIL → implement → PASS (live skipped without keys).
- [ ] **Step 5: Commit** `feat: add LiteLLM routing, typed LLM client and cost accounting`

---

### Task 3: Caps and contract-shaped events

**Files:**
- Create: `backend/src/sluice/agent/caps.py`, `backend/src/sluice/agent/events.py`, `backend/tests/agent/test_events.py`

**Interfaces:**
- `caps.py`: `MAX_ATTEMPTS = 3`, `MAX_TOKENS_PER_FILE = 60_000`, `MAX_WALL_S_PER_FILE = 180`, `SANDBOX_TIMEOUT_S = 20`, `SANDBOX_MEM_MB = 512`, `SANDBOX_PIDS = 64`, `MAX_ERROR_CHARS = 2_000`.
- `events.py`: Pydantic models mirroring `RunEvent` in `docs/api/contract.md` (camelCase via alias generator; read the contract from the UI branch `feat/v1-ui-shell` if not yet merged): run_started, profiled, rules_applied, plan_created, node_started (with `maxAttempts`), attempt_failed (with `tokens`, `costUsd`, error ≤ `MAX_ERROR_CHARS`), node_finished, verified, loaded, run_finished; discriminated union `PipelineEvent`; `EventLog.emit(event)` assigns strictly increasing `seq` from 1 and ISO `at`; `EventLog.events`.

- [ ] **Step 1: Write failing tests:** JSON field names match the contract (`costUsd`, `maxAttempts`, `runId` …); seq strictly increasing; error truncated to 2,000 chars; union round-trips.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: add agent caps and contract-shaped pipeline events`

---

### Task 4: Sandbox runner

**Files:**
- Create: `backend/sandbox/Dockerfile`, `backend/sandbox/run_transform.py`, `backend/src/sluice/sandbox/runner.py`, `backend/tests/sandbox/test_runner_unit.py`, `backend/tests/integration/test_sandbox.py`
- Modify: `Makefile` (`sandbox-image`), `backend/pyproject.toml` (`uv add docker`)

**Interfaces:**
- Image `sluice-sandbox:dev`: `python:3.14-slim` + the exact `polars` version from `uv.lock`; non-root user uid 10001; entrypoint `python /app/run_transform.py`.
- In-container contract: `/work/in/transform.py` defines `transform(values: pl.Series) -> pl.Series`; `/work/in/values.parquet` has one String column `values`; the runner writes `/work/out/values.parquet` (one String column, same length) or `/work/out/error.txt`.
- `SandboxResult(ok: bool, values: pl.Series | None, error: str | None, duration_ms: int)`.
- `run_transform(code: str, values: pl.Series, *, timeout_s: int = SANDBOX_TIMEOUT_S, client: docker.DockerClient | None = None) -> SandboxResult` — temp dir with code + parquet; `containers.run(image, network_disabled=True, read_only=True, user="10001", cap_drop=["ALL"], security_opt=["no-new-privileges"], mem_limit=f"{SANDBOX_MEM_MB}m", pids_limit=SANDBOX_PIDS, tmpfs={"/tmp": "size=64m"}, volumes={in: ro, out: rw}, environment={}, detach=True)`; wait with timeout, kill on timeout; always remove container and temp dir; error ≤ `MAX_ERROR_CHARS`.

- [ ] **Step 1: Write failing tests.** Unit (fake client): container removed even when wait raises; timeout → kill, `ok=False`, error mentions "timeout"; length mismatch → `ok=False`. Integration: identity round-trip; `"1,24,500.50" → "124500.50"` works; hostile transforms fail contained — HTTP request (network), write to `/work/in` and `/etc` (read-only), environment has no `GROQ_API_KEY`/`GEMINI_API_KEY`, fork bomb (pids), `bytearray(2_000_000_000)` (memory), `while True: pass` (timeout).
- [ ] **Step 2:** FAIL. **Step 3:** Implement; `make sandbox-image`. **Step 4:** unit PASS in `make test`; integration PASS in `make test-integration`.
- [ ] **Step 5: Commit** `feat: add hardened sandbox for generated transforms`

---

### Task 5: Schema mapping

**Files:**
- Create: `backend/src/sluice/agent/mapping.py`, `backend/tests/agent/test_mapping.py`

**Interfaces:**
- Consumes: `LLM.run`, `match_columns()` (rules' exact/normalised matching), `Contract`.
- `MappingProposal(mappings: list[MappingItem])`, `MappingItem(source: str, target: str | None, reason: str)` (LLM output); `ColumnMapping(pairs: dict[str, str | None])`.
- `async propose_mapping(df: pl.DataFrame, contract: Contract, llm: LLM) -> tuple[ColumnMapping, list[CallRecord]]` — asks only about source columns `match_columns` couldn't match and contract columns still missing; prompt has each unmatched column's name + ≤ 8 samples and each missing column's spec (name, dtype, unit, enum). Tier `small`; on validation failure one retry on `strong` with the error; unresolved stays unmapped (rules then flag missing columns — never invent).
- `validate_mapping(proposal, df, contract, already_matched) -> ColumnMapping` raising `MappingError`: targets ∈ contract; no target used twice (incl. already-matched); sources exist.
- `apply_mapping(df, mapping) -> pl.DataFrame` — rename mapped sources, drop `None` targets.

- [ ] **Step 1: Write failing tests** (FunctionModel): `amt` → `o_totalprice`; `amt_paise` → `o_totalprice`; hallucinated target rejected then retried on `strong`; duplicate target rejected; all-matched input → no LLM call; two sources for one target after retry → both unmapped.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: add LLM schema mapping with strict validation`

---

### Task 6: Planner and router

**Files:**
- Create: `backend/src/sluice/agent/plan.py`, `backend/tests/agent/test_plan.py`

**Interfaces:**
- `PlanNode(id: str, column: str, issue: str, difficulty: Literal["easy","hard"], depends_on: list[str])`; `Plan(nodes: list[PlanNode])`.
- `candidate_columns(raw: pl.DataFrame, rules: RuleResult, contract: Contract) -> dict[str, list[str]]` — pure code: contract columns with flagged cells whose raw text is not a destroyed token, each with ≤ 12 raw samples of those cells.
- `async propose_plan(candidates, contract, llm) -> tuple[Plan, list[CallRecord]]` — orchestrator, tier `strong`; empty candidates → `Plan(nodes=[])`, no call. Validation: column ∈ candidates, ≤ 1 node per column, `depends_on` ids exist and are acyclic, ids `[A-Za-z0-9_-]{1,64}`.
- `route_tier(node: PlanNode, attempt: int) -> Literal["small","strong"]` — static cascade: `easy` → `small` on attempt 1 then `strong`; `hard` → `strong`. The router alone picks the tier (spec §11 item 1).

- [ ] **Step 1: Write failing tests:** columns whose flagged cells are all destroyed tokens excluded; Indian-comma `o_totalprice` included; no candidates → no call; non-candidate column rejected; cycle rejected; `route_tier` table.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: add repair planner and static cascade router`

---

### Task 7: Worker, fabrication guard, verifier

**Files:**
- Create: `backend/src/sluice/agent/worker.py`, `backend/src/sluice/agent/verifier.py`, `backend/tests/agent/test_worker.py`, `backend/tests/agent/test_verifier.py`

**Interfaces:**
- `TransformCode(code: str, explanation: str)`.
- `async write_transform(node: PlanNode, spec: ColumnSpec, source_name: str, samples: list[str], prior_error: str | None, tier: Tier, llm: LLM) -> tuple[TransformCode, CallRecord]` — prompt states: define `transform(values: pl.Series) -> pl.Series`, String → String, same length, return null for anything unrecoverable, output text the rules parse for `spec.dtype` (plain decimal `"124500.50"`, ISO date, ISO-8601 with offset), plus column spec, source column name (hints like `_paise`), samples and `prior_error`.
- `guard_output(raw: pl.Series, out: pl.Series) -> str | None` — error when lengths differ, or any position where `raw` is null or a destroyed token (`"N/A"`, `""`, `"-1"`, `"###"`) has a non-null `out` (**fabrication guard**); else None.
- `Verdict(accept: bool, reason: str)`; `async verify_node(node, pairs: list[tuple[str, str | None]], stats: dict[str, float | int | None], llm) -> tuple[Verdict, CallRecord]` — independent verifier on `strong`, sees ≤ 20 raw → rewritten pairs and before/after stats (parsed min/max/median, null counts) and judges plausibility (e.g. paise left 100× too large → reject).

- [ ] **Step 1: Write failing tests:** guard rejects `"N/A"→"0.00"` and `null→"1"`, accepts nulls for tokens, rejects length mismatch; prompt contains dtype, source name and `prior_error` (captured via FunctionModel); verifier returns the model's verdict.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: add worker transforms, fabrication guard and verifier`

---

### Task 8: Pipeline state machine and `agent` eval system

**Files:**
- Create: `backend/src/sluice/agent/pipeline.py`, `backend/src/sluice/systems/agent.py`, `backend/tests/agent/test_pipeline.py`
- Modify: `backend/src/sluice/evals/runner.py` (`SYSTEMS["agent"] = AgentSystem`; `limit: int | None` on `run_eval` — first N cases by id, recorded in the summary and shown in RESULTS.md as "subset n=N"), `backend/src/sluice/cli.py` (`--limit`)

**Interfaces:**
- `PipelineDeps(llm: LLM, sandbox: Callable[[str, pl.Series], SandboxResult], clock: Callable[[], float])`.
- `async run_pipeline(case: CaseInput, deps: PipelineDeps, log: EventLog) -> SystemOutput` — read_input → `propose_mapping` → `apply_mapping` → profile + `apply_rules` (pass 1) → `candidate_columns` → `propose_plan` → nodes in dependency order (independent ones concurrently via `asyncio.gather`), each: up to `MAX_ATTEMPTS` × (`route_tier` → `write_transform` → `deps.sandbox` → `guard_output` → `verify_node`; failures feed `prior_error`) → replace accepted columns in the raw frame → profile + `apply_rules` (pass 2) → `SystemOutput(df, flags, quarantined, file_quarantined, cost_usd=Σ records, latency_s, meta={"plan", "nodes", "records", "llm_error"?})`. Emits contract events in order. `MAX_TOKENS_PER_FILE` / `MAX_WALL_S_PER_FILE` stop new attempts but keep accepted work. `LLMUnavailable`/`LLMOutputError` → that step falls back to rules-only + `meta["llm_error"]`; never raises for model/provider problems.
- `AgentSystem` (`name = "agent"`): `run(case)` = `asyncio.run(run_pipeline(...))` with the real `LLM` (from `Settings`) and real sandbox.

- [ ] **Step 1: Write failing tests** (scripted FunctionModel + fake sandbox mapping code → canned outputs): on a built medium case (rename + Indian numbers) `grade` cell accuracy > RulesOnly's and fabrication 0; failing attempt 1 retried with escalation `small`→`strong`; stops at `MAX_ATTEMPTS`; `LLMUnavailable` during planning → output equals RulesOnly's and `meta["llm_error"]` set; events in contract order, strictly increasing seq, exactly one `run_finished`; no candidates → no plan call and `plan_created.nodes == []`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: add agent pipeline and register agent eval system`

---

### Task 9: Iceberg load and lineage

**Files:**
- Create: `backend/src/sluice/load/iceberg.py`, `backend/tests/integration/test_load.py`
- Modify: `backend/src/sluice/cli.py` (`sluice run <file> --table <orders|lineitem>`: runs the pipeline, prints events as lines, loads output), `backend/pyproject.toml` (`uv add "pyiceberg[…]"` with the extras current docs require for a Postgres SQL catalog + PyArrow + S3)

**Interfaces:**
- `load_output(out: SystemOutput, contract: Contract, *, run_id: str, file_sha256: str, settings: Settings) -> LoadResult(snapshot_id: str | None, rows_loaded: int, rows_quarantined: int, skipped: bool)` — SQL catalog on Postgres, warehouse `s3://sluice-warehouse/`; tables `sluice.<name>` (contract schema) and `sluice.<name>_quarantine` (PK text + reason); idempotency key `(file_sha256, contract.version)` in Postgres `loads` — a repeat returns `skipped=True` and writes nothing.
- Lineage (created `IF NOT EXISTS`): `runs(run_id, file_sha256, table_name, started_at, finished_at, status, cost_usd)`, `plan_nodes(run_id, node_id, column_name, tier, attempts, accepted)`, `transforms(run_id, node_id, attempt, code_sha256, code, model, passed)`, `loads(run_id, file_sha256, contract_version, snapshot_id, rows_loaded, rows_quarantined)`; `rollback(table, snapshot_id)` helper.

- [ ] **Step 1: Write failing integration tests:** clean orders frame → snapshot with its rows; same file again → `skipped=True`, count unchanged; quarantine rows in the quarantine table; lineage rows present; rollback restores the previous snapshot.
- [ ] **Step 2–4:** FAIL → implement → PASS (`make up`, `make test-integration`).
- [ ] **Step 5: Commit** `feat: load accepted output into Iceberg with lineage and idempotency`

---

### Task 10: First agent results (owner-assisted)

**Files:**
- Modify: `evals/RESULTS.md` (generated), `README.md` (results table gets a real agent row marked as a dev subset), this plan (record chosen models, rpm/tpm, price-table date)

**Prerequisites (owner):** `backend/.env` filled; `make up`. Optional local small model: owner approves installing **vllm-metal** in a project-local venv and downloading `mlx-community/Qwen3.5-4B-MLX-4bit` (~2.5 GB), served on 58000 — without it, `small` falls back to Groq.

- [ ] **Step 1:** `make sandbox-image && make test-integration` PASS; `uv run python -m pytest -m live` PASS.
- [ ] **Step 2:** `cd backend && PYTHONPATH=src uv run sluice eval --system agent --split dev --trials 1 --limit 30`, the same for `rules`, then `sluice results`. RESULTS.md shows both rows on the same 30 cases with cost and the subset size. Fabrication must be 0.0 — if not, stop and report the cases.
- [ ] **Step 3:** Report: models used, total spend, failures by step, and the cache hit rate measured with a repeat `sluice run` of one file. Eval trials bypass the LiteLLM response cache (Ruling F3), so a second eval run is not a cache check.
  - Pick one dev case and run `cd backend && PYTHONPATH=src uv run sluice run <case input> --table <table>`.
  - A plain repeat is skipped before the pipeline runs, because the file's idempotency key is recorded. So first delete that key row: `docker compose --env-file backend/.env exec postgres psql -U sluice -d sluice -c "DELETE FROM loads WHERE file_sha256 = '<sha256 of the file>'"`.
  - Then run the same command again. The pipeline runs in full. The loader finds the earlier Iceberg commit in the snapshot ancestry and appends nothing; its summary says "recovered an earlier commit".
  - Expect ≈ 100% cached calls and $0 on the second run (`tokens` and `costUsd` in its `run_finished`).
- [ ] **Step 4: Commit** `docs: publish first agent results on a dev subset`

---

## Done when

- `make test` (no services, no keys) passes locally and in CI; `make test-integration` passes with `make up`; `pytest -m live` passes with keys.
- `sluice eval --system agent` runs end to end on real models with 0 fabrication, every transform executed only in the sandbox, and RESULTS.md shows agent vs rules on the same cases.
- `sluice run <file> --table orders` loads into Iceberg idempotently with lineage.
