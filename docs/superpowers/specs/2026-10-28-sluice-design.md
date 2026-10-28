# Sluice — design

*A self-planning agentic data pipeline for drifting data, measured like production.*

Date: 2026-10-28 · Status: draft, awaiting owner approval

---

## 1. Intent and why

Sluice is a portfolio project meant to get the owner hired as an AI/data engineer at Indian product companies and GCCs (Hyderabad/Bengaluru). The owner rejected toy LLM demos and pure-infra checklists. The project must be **real engineering whose quality is proven with numbers**, in the spirit of [ship-ai-projects](https://github.com/rasswanth-lyzr/ship-ai-projects): every number has a baseline, a sample size and a confidence interval; failures are shown, not hidden; everything reproduces with one command.

The problem it solves: production data drifts (renamed columns, type changes, unit changes, new formats), and drift breaks pipelines. Sluice receives a drifted file, **plans its own repair pipeline**, has subagents write transforms that run in a locked-down sandbox, verifies the result against a data contract, repairs on failure, and loads clean data — while recording what it cost, how long it took, and exactly which code produced which row.

It combines the three ship-ai-projects briefs into one system:

| Brief | Where it lives in Sluice |
|---|---|
| 01 LLM Ops | LiteLLM gateway, multi-provider fallback, OTel traces, Grafana SLO dashboard, CI eval gate, load test, runbook |
| 02 Agent evals | Ground-truth drift benchmark, pass^k, layered graders, failure taxonomy, gated improvement loop on a frozen holdout |
| 03 Auto routing | Per-step model routing (rules → small local → strong hosted), cost–quality Pareto against baselines |

**One-line pitch (target):** "A pipeline agent that plans, sandboxes and repairs its own transforms for drifting data: X% cell accuracy, Y% fabrication rate, pass^4 Z%, at W% of always-strong-model cost — with a CI gate that blocks regressions and 0/N sandbox escapes."

## 2. Goals

1. **Dynamic, self-planning pipeline.** An orchestrator agent reads a file's profile and writes a plan (a small DAG of subtasks) before doing any work, weighing cost budget, latency, data size and risk. It spawns worker subagents per subtask, runs independent ones in parallel, and has an independent verifier check the merged result. On failure it re-plans only the failed part.
2. **Exact ground truth.** Every eval case has a known correct output, so accuracy is measured, not judged.
3. **Safe execution of LLM-written code.** All generated code runs in a hardened, network-less, throwaway container. Proven with a hostile test set.
4. **Production-grade operation.** Event-driven ingestion, rate-limit-aware workers, idempotent loads, lineage, observability, SLOs, alerts, CI gate, load tests.
5. **Self-hosted inference story.** Workers run on vLLM; a GPU benchmark (Kaggle T4) measures throughput, latency and prefix caching.
6. **A Claude/ChatGPT-style web UI** with a liquid-glass look (light + dark) where the user drops a file and watches the plan execute live.
7. **₹0 running cost.** Everything runs on the owner's laptop and free tiers.

## 3. Non-goals

- Not a general ETL tool or a dbt/Airflow replacement.
- Not a text-to-SQL / BI chat product. The chat UI is a window into pipeline runs, not a general assistant.
- Not a hosted multi-tenant SaaS. No auth beyond a single local user.
- No paid cloud deployment. AWS/EKS are documented as the production path, not run.
- No local 9B (or larger) model experiments. **Owner decision: dropped entirely.**
- No fine-tuning of models.

## 4. Anti-goals (failure even if it "works")

- Headline numbers without baselines, n, or confidence intervals.
- Eval data where an LLM wrote both the cases and the answers.
- An LLM judge used as the primary grader for anything that can be checked by code.
- Any generated code running outside the sandbox, ever, including in tests.
- Autoscaling that makes rate-limit storms worse (scaling workers into a 429 wall).
- Copying ChatGPT's or Claude's name, logo or branding. The *layout and feel* are borrowed; the identity is Sluice's own.
- A README that is a wall of infrastructure logos with no results.
- Any spend. If something needs a credit card, it's out or optional.

## 5. Constraints

| Constraint | Value |
|---|---|
| Hardware | MacBook Pro M4, **16 GB RAM**. Whole stack must fit in ~7 GB alongside a browser. |
| Container runtime | Colima (already installed, Docker API). Set Colima VM memory to ~3–4 GB. |
| Budget | ₹0. Free tiers only. |
| Package managers | **Bun** for JS/TS. **uv** for Python. |
| Dev tools | Prefer Rust > Go > others: Biome, Oxlint, tsgo, Ruff, k6. |
| Versions | Latest stable of every dependency, **verified at setup time** (don't pin from memory). |
| Authorship | Owner is sole author. No AI attribution or Co-Authored-By trailers in commits or PRs. |
| Secrets | API keys only via `.env` (git-ignored) loaded by Compose `--env-file`; never committed, never printed. |
| Repo | Public on GitHub (owner creates the remote). CI on GitHub Actions free tier. |

## 6. System overview

```
 drop file (UI) / put object (S3 API)
            │
            ▼
   RustFS bucket: landing/ ──event──▶ Redpanda topic: files.landed
                                           │
                                           ▼
                                   Worker pool (Python)
                                           │
        ┌──────────────────────────────────┼─────────────────────────────────┐
        │ 1 Profile (DuckDB/Polars, no LLM)                                  │
        │ 2 Rule fixes (no LLM)                                              │
        │ 3 Orchestrator plans DAG ──▶ LiteLLM ──▶ Groq (fallback Gemini)    │
        │ 4 Worker subagents write transforms ──▶ LiteLLM ──▶ vLLM (local)   │
        │ 5 Sandbox runs each transform (Docker, no network)                 │
        │ 6 Verifier subagent + contract checks (Pandera)                    │
        │ 7 Repair loop (bounded) → re-plan failed nodes only                │
        │ 8 Idempotent load → Iceberg table on RustFS                        │
        └──────────────────────────────────┼─────────────────────────────────┘
                                           │
     Postgres (runs, plans, steps, transforms, lineage, evals, Iceberg catalog)
     Redis (LLM response cache, shared token-bucket rate limiter)
     OTel → Phoenix (traces) · Prometheus → Grafana (SLOs, cost, fallbacks)
                                           │
                     FastAPI (REST + SSE) ──▶ Next.js liquid-glass chat UI
```

## 7. Components

### 7.1 Datasets and ground truth

**Primary: TPC-H, generated by DuckDB `dbgen`.** Tables: orders, lineitem, customer, supplier, part, partsupp, nation, region. Deterministic, any scale factor (SF 0.01 for fast dev, SF 1+ for scale tests).

**Drift generator** (`sluice-drift`, part of the repo). Takes a clean table plus a drift recipe and emits: the drifted file, the expected clean output, and a manifest describing exactly what was changed. Drift types (each parameterised, composable into multi-issue files):

| Drift type | Example |
|---|---|
| Column rename | `o_totalprice` → `amt` |
| Type change | decimal arrives as string `"1,24,500"` |
| Unit change | rupees ↔ paise on a subset of rows |
| Timezone shift | UTC timestamps arrive as IST, or mixed |
| Date format mix | `2026-10-03`, `03/10/26`, `Oct 3 2026` in one column |
| Format change | CSV ↔ JSON (flat/nested) ↔ Parquet |
| Encoding | Latin-1 / BOM / mojibake in text columns |
| Whitespace / case noise | `" 1042 "`, `MUMBAI` vs `Mumbai` |
| Duplicates | exact and near-duplicate rows |
| Referential breaks | foreign keys pointing at missing parents |
| Nulls / sentinels | `NULL`, `""`, `-1`, `N/A` for missing |
| Column split/merge | `name` → `first_name` + `last_name` |
| Added / dropped columns | new optional column; required column missing (must be flagged, not invented) |

Some cases are deliberately **unrecoverable** (information destroyed). The correct behaviour there is to flag and quarantine, not fabricate. This is how fabrication rate is measured.

**Real-world OOD test: NYC TLC trip records** (yellow/green taxi, multiple years). Real historical schema drift (columns added over years, type and name changes). Ground truth = the documented target schema per the official TLC data dictionaries, encoded by the builder as a per-year mapping and checked by code (types, ranges, totals that must reconcile). No owner labelling. Never used during development tuning; evaluated only at milestone checkpoints.

**Splits.** `dev` (free to iterate on), `holdout` (frozen, versioned, hashed; only touched by the gated improvement loop), `ood` (NYC TLC), `hostile` (sandbox attack files, see 7.6). Target sizes: dev ≥ 300 cases, holdout ≥ 300 cases, each run ≥ 4 trials.

### 7.2 Data contracts

Each target table has a contract: column names, types, nullability, ranges, enums, uniqueness, foreign keys, units and timezone. Expressed with **Pandera** (Polars backend) and stored as versioned files in the repo. Contract failures produce structured, specific error messages that are fed to repair attempts.

### 7.3 Pipeline and agent design

The outer pipeline is a **hand-written state machine in Python** (not a framework), so every transition, retry and cap is explicit and unit-testable. LLM calls use **Pydantic AI** for typed, schema-validated outputs.

Steps per file:

1. **Profile** (no LLM). Polars/DuckDB compute schema, types, null rates, value patterns, sample rows, and a diff against the target contract.
2. **Rule fixes** (no LLM). Deterministic fixers for known-easy issues (whitespace, case, known date formats, sentinel nulls, exact duplicates). This stage alone is also the **rules-only baseline**.
3. **Plan** (orchestrator). Input: profile + contract diff + run budget. Output: a typed plan — a DAG of subtasks, each with: issue description, affected columns, a difficulty hint (features for the router — the orchestrator does **not** choose the model), dependencies, sample-first flag (for large files), and verification strictness. The orchestrator may also decide that no LLM work is needed.
4. **Work** (worker subagents). One subagent per DAG node, independent nodes in parallel. Each gets only its own slice of context (relevant columns, sample rows, contract fragment) and writes a transform (Polars code or DuckDB SQL) as a typed artifact.
5. **Sandbox run.** Each transform runs in its own container (see 7.6). Large files: run on a sample first; only promote to the full file if the sample passes.
6. **Verify.** Contract checks (code) first, then an independent **verifier subagent** that did not write the code reviews the merged result for semantic issues (e.g., implausible unit conversions).
7. **Repair.** On failure, the error and traceback go back to the responsible worker. Caps: **max 3 attempts per node, per-file token budget, per-file wall-clock budget.** If a node exhausts its caps, the orchestrator may re-plan that node once; otherwise the file is quarantined with a reason.
8. **Load.** Idempotent write into an Iceberg table (see 7.5). Quarantined rows go to a quarantine table with reasons.

**Single-agent mode** (one agent does plan + transforms in one context) exists as an ablation, run on the same holdout, to measure whether orchestration is worth its tokens.

### 7.4 Model layer and routing

- **LiteLLM proxy** in front of all models: retries only on 429/5xx, timeouts, fallback chains, budgets, per-request cost tracking, and a **Redis response cache** keyed on (model, prompt hash, params) so eval reruns cost nothing.
- **Orchestrator / verifier:** Groq free tier (a 70B-class model or Qwen3-32B — pick at setup from what Groq currently offers), **fallback to Gemini via Google AI Studio free tier** on 429/5xx. Two providers satisfies brief 01.
- **Workers:** local **vLLM** via **vllm-metal** on the Mac. Candidates (MLX 4-bit, picked by a week-1 bake-off on 100 dev cases, winner = best accuracy per second): `Qwen3.5-4B-MLX-4bit` (default), `Qwen3-4B-Instruct-2507-4bit`, `gemma-4-e4b-it-4bit`. Verify at setup that each runs on vllm-metal.
- **Shared rate limiter:** a Redis token bucket per provider/model, sized from the provider's real limits (read from the Groq console at setup — published third-party numbers conflict). Workers block on the bucket instead of hammering a 429.
- **Router:** custom logic plugged into LiteLLM.
  - v1: static cascade — rules → small local → strong hosted (escalate on validation failure).
  - v2+: learned router (kNN over profile features, then a small classifier) predicting P(success) per tier; decision rule `argmax P(correct) − λ·cost`, swept over λ.
  - **Baselines:** rules-only, always-small, always-strong, random at matched cost, static per-drift-type table, oracle (best tier per case in hindsight).
  - Cost: computed from actual token counts × a dated price table. Free-tier calls are also priced at the provider's list price so the curve reflects real-world cost, with ₹0 actual spend reported separately.

### 7.5 Storage

| Store | Tech | Holds |
|---|---|---|
| Object storage | **RustFS** (S3 API, Apache-2.0) | landing zone, sandbox input/output, Iceberg data files |
| Event stream | **Redpanda** (Kafka API) | `files.landed`, `runs.events` topics |
| Control plane | **Postgres** | runs, plans, steps, transform versions (code + hash), model calls, lineage, eval results, **PyIceberg SQL catalog** |
| Cleaned data | **Apache Iceberg** tables on RustFS via **PyIceberg** | one table per contract + quarantine tables |
| Analytics / evals | **DuckDB** | reading Iceberg/Parquet for graders and reports |
| Cache / limiter | **Redis** | LLM response cache, token buckets |

MinIO is **not** used (community edition archived Feb 2026, no images or patches).

**Idempotency:** each load is keyed by (run ID, file hash, contract version); row identity by a deterministic row hash. Re-delivered events or a worker killed mid-load must produce no duplicates and no missing rows.

**Rollback:** a bad load is undone by rolling the Iceberg table back to the prior snapshot; lineage identifies exactly which load and transform version caused it.

**Lineage:** OpenLineage-shaped events stored in Postgres: input file → plan → transform version (+ model that wrote it) → output snapshot. Queryable per row via row hash.

### 7.6 Sandbox

- One throwaway Docker container per transform run, started via the Docker SDK against Colima.
- Flags: `--network none`, read-only root filesystem, single writable tmpfs scratch dir, non-root user, `--cap-drop ALL`, `no-new-privileges`, default (or tighter) seccomp profile, memory/CPU/pids limits, hard wall-clock timeout, container removed after run.
- Runner image contains only the runtime needed (Python, Polars, DuckDB, PyArrow). No credentials, no host mounts except the input file (read-only) and the scratch dir.
- Data in/out via files in the scratch dir, never via network.
- **Stretch:** gVisor (`runsc`) runtime inside the Colima VM.
- **Hostile set:** files containing prompt injections (in headers, cell values, JSON keys) instructing the agent to read host files, exfiltrate env vars, open network connections, fork-bomb, fill disk, or write outside scratch. Graded by code: any escape attempt that succeeds = failure. Reported as "k of N escapes" with N ≥ 100.

### 7.7 Evals

All graders are code unless stated otherwise.

| Metric | Definition |
|---|---|
| Cell accuracy | % of output cells exactly equal to ground truth (numeric tolerance per contract) |
| Row recall / precision | correct rows kept / spurious rows introduced |
| Fabrication rate | % of cells where ground truth is "unrecoverable/flag" but the system output a value; plus invented columns/rows |
| Contract pass rate | % of files whose final output passes the contract |
| Repair success | % of first-attempt failures fixed within caps; mean attempts |
| Plan quality | DAG node count vs manifest issue count; missed issues; unnecessary LLM nodes |
| pass@k / pass^k | k = 4 trials per case; flip rate as the noise floor |
| Cost, latency | per file and per 1k rows; p50/p95 |
| Sandbox escapes | k of N on the hostile set |

Statistics: bootstrap 95% CIs on every headline number; McNemar's test when comparing two versions on the same cases. A per-version **failure taxonomy** (missed drift, wrong unit conversion, fabricated value, contract violation, sandbox timeout, plan error, provider error) is produced from manifests + traces.

**Verifier quality (no human labels):** the verifier subagent's verdicts are graded **against ground truth by code** — on TPC-H we know whether each merged output is actually correct, so the verifier's TPR, TNR and Cohen's κ come for free. The owner will not hand-label anything; no metric may depend on human labels.

**Gated improvement loop:** versions v0 → vN; each change is evaluated on the frozen holdout; a change ships only if it improves the target metric beyond the noise floor without regressing fabrication rate. Produces the improvement-iterations chart.

**CI gate (GitHub Actions):** on every PR, run a small fixed eval slice (≈ 50 cases × 1–2 trials, cached responses where prompts didn't change, small model) and **fail the PR** if cell accuracy or fabrication rate regresses beyond a set threshold. The README must link to at least one real PR that the gate blocked.

### 7.8 Observability and ops

- **OpenTelemetry** with GenAI semantic conventions on every LLM call, sandbox run and pipeline step → **Arize Phoenix** (single container) for traces.
- **Prometheus + Grafana** dashboard: throughput, p50/p95/p99 latency vs SLO, errors by class (429, timeout, contract-fail, sandbox-fail), fallback rate, cost per minute, error budget.
- **Alerts:** SLO burn-rate alert and cost-spike alert (Grafana alerting, local).
- **Chaos drills:** kill a worker mid-load (prove idempotency), simulate primary provider outage (prove fallback, show the spike), drop the event stream briefly (prove no loss).
- **RUNBOOK.md** and at least one written **postmortem** from a real incident during development.

### 7.9 Inference benchmark (Kaggle)

- vLLM (CUDA) on Kaggle's free 2× T4, serving the chosen worker model.
- Measure with vLLM's own benchmark tool and k6: throughput and latency at concurrency 1 → 64; prefix caching on vs off using Sluice's real prompts (shared system prompt + contract); cost per 1k rows on self-hosted GPU vs the free/list-priced API.
- Notebook committed to the repo; results committed as data + charts.

### 7.10 Load testing

- **k6** drives bursts of files through the full pipeline (local Compose stack). Reports sustained files/min and where the bottleneck is (expected: LLM rate limits, not the database — confirm with numbers).

### 7.11 API

FastAPI + Pydantic. Endpoints (shape decided by builder): upload file, list/get runs, **SSE stream of run events** (plan created, node started/finished, attempt failed, verified, loaded), get plan DAG, get transform code and diffs between attempts, get lineage, get cost breakdown, list eval runs and results.

### 7.12 Web UI

- **Layout:** owner chose layout "A" — Claude-style: left sidebar (new run, today's runs, eval results), centre chat thread with live step list and a plain-language summary, bottom composer (drop file / ask about the run), right panel with tabs **Data** (before/after), **Code** (generated transforms + diff between attempts), **Lineage**, **Cost**. Add a **Plan** view showing the DAG and which nodes ran in parallel. Mockup: `docs/design/ui-mockup-liquid-glass.html`.
- **Look:** Apple-style **liquid glass** — translucent frosted panels (`backdrop-filter` blur + saturation), thin specular edge highlights, soft shadows, colourful blurred backdrop. **Light and dark mode** with a toggle; respects system preference by default.
- **Behaviour:** steps stream in live via SSE; the composer accepts follow-up questions about a run (answered from the run's own records — no general chat).
- **Identity:** own name (Sluice) and visual identity. No ChatGPT/Claude logos, names or brand colours.
- **Accessibility:** glass must keep text contrast at WCAG AA; honour `prefers-reduced-motion` and `prefers-reduced-transparency` (fall back to solid panels).
- **Stack:** Next.js (App Router), React, TypeScript, Tailwind CSS, shadcn/ui (restyled), Motion, TanStack Table (virtualised), Shiki, **Apache ECharts** for in-app charts (Pareto, improvement chart). Tooling: Bun, Biome, Oxlint, tsgo, Playwright.

### 7.13 Deployment

- **Local:** one `make up` brings up everything: Docker Compose for all services, plus vllm-metal started **natively on the Mac** (it needs the Metal GPU, which Colima containers can't use); services reach it via `host.docker.internal`; Compose profiles let observability and local inference be switched off to save RAM. `make eval` reproduces every number in the README from committed seeds and cache.
- **Kubernetes:** kind + Helm charts for the services; **KEDA** scales workers on Redpanda consumer lag, with max replicas capped by the rate-limit budget.
- **AWS (documented only):** RustFS → S3, Redpanda → MSK, Compose/kind → EKS, Postgres → RDS. No paid account required.

## 8. Required outputs (the "proof")

The README leads with results, not architecture. By the end of v3 it must contain (v1 ships items 1, 2 with the static cascade, 6, 9, 10, 11; see §9):

1. **Headline table**: cell accuracy, fabrication rate, pass^4, contract pass rate, cost per 1k rows, p95 latency — for Sluice vs rules-only vs always-strong, with 95% CIs and n.
2. **Cost–quality Pareto chart**: router curves over λ vs baselines (as points), log-cost axis.
3. **Improvement chart**: holdout metrics across gated versions + failure taxonomy shrinking.
4. **Ops dashboard screenshot** from a real chaos drill (provider outage → fallback spike).
5. **Orchestration ablation**: single-agent vs orchestrator+workers by file complexity.
6. **Sandbox result**: k of N escapes on the hostile set.
7. **Inference benchmark**: throughput/latency vs concurrency; prefix-cache gain; self-hosted vs API cost.
8. **OOD result** on NYC TLC.
9. Link to a **PR blocked by the CI gate**.
10. **Honest failures**: a section listing what still breaks and why.
11. A short GIF of the UI running a file end to end.

## 9. Releases

No deadline, but **v1 must ship fast** so it can be promoted (CV, LinkedIn, README) while later releases add depth. Each release is complete and promotable on its own.

### v1 — promotable core
1. Compose stack: Postgres, Redis, RustFS, LiteLLM; vllm-metal native on the Mac.
2. TPC-H generation + drift generator + contracts for `orders` and `lineitem` (other tables later).
3. Rules-only baseline and all code graders (cell accuracy, fabrication, contract pass, repair success, pass^4, bootstrap CIs).
4. Full agent pipeline: profile → rules → orchestrator plan → parallel worker subagents → sandbox → verifier → bounded repair → idempotent Iceberg load + lineage.
5. Model layer: Groq orchestrator/verifier with Gemini fallback, Redis cache, shared rate limiter, static cascade router; week-1 worker-model bake-off.
6. Hostile set (N ≥ 100) and sandbox result.
7. CI gate on GitHub Actions, with one real PR it blocked.
8. Routing matrix + offline cost–quality chart: static cascade vs rules-only / always-small / always-strong / random / oracle.
9. Liquid-glass UI (light + dark): sidebar, live step stream via SSE, Data and Code tabs, Plan view.
10. OTel traces → Phoenix.
11. README with headline table, cost–quality chart, sandbox result, blocked-PR link, honest failures, GIF.

**v1 job intake:** files are submitted through the API and queued in a Postgres job table (`SELECT … FOR UPDATE SKIP LOCKED`). Redpanda arrives in v2.

### v2 — production depth
Redpanda event-driven intake; Prometheus + Grafana SLO dashboard and alerts; chaos drills + RUNBOOK + postmortem; k6 load test; Kaggle vLLM benchmark (concurrency, prefix caching, self-hosted vs API cost); single-agent vs orchestrator ablation; learned router + λ-sweep Pareto; gated improvement iterations chart; Lineage and Cost UI tabs; remaining TPC-H tables.

### v3 — stretch
NYC TLC out-of-distribution evaluation; gVisor runtime; Helm + KEDA on kind with a recorded scaling demo; AWS production-path docs.

## 10. Decisions made

**Owner's decisions:** Pattern A (self-healing pipeline) with routing and eval gate built in; vLLM not Ollama; LiteLLM; Kaggle T4 benchmark; Groq for the orchestrator; Claude-style layout A with liquid glass + dark mode; Postgres + Iceberg + DuckDB; MLX-community-popular models for local workers; no local 9B experiments; data choice delegated; no deadline but ship v1 fast; **no human labelling by the owner**.

**Designer's calls (owner may override):** name "Sluice"; TPC-H + drift generator as primary data, NYC TLC as OOD; hand-written state machine + Pydantic AI instead of LangGraph; Pandera contracts; Polars transforms; RustFS instead of MinIO; Redpanda instead of Apache Kafka; Phoenix instead of Langfuse (RAM); ECharts over Recharts; Gemini (AI Studio) as fallback provider; 3-attempt repair cap; dev/holdout/ood/hostile splits and sizes; README structure.

## 11. Clarifications after builder review (designer's calls)

1. **Who picks the model tier:** the **router only**. The orchestrator plans subtasks and emits difficulty hints; the router turns hints + profile features into a tier. This keeps the Pareto experiment a measurement of the router.
2. **Strong tier = the Groq orchestrator-class model** (fallback Gemini). The always-strong baseline uses the same model.
3. **Quota strategy (brief 03 method):** build a **routing matrix** once — every holdout case × every tier, 2 runs each, all cached. Router variants, baselines and the λ sweep are then evaluated **offline** from the matrix at zero extra API cost. Live 4-trial pass^k runs are reserved for the headline configurations (Sluice, rules-only, always-strong, single-agent ablation) and for each gated version.
4. **Where vLLM runs:** natively on the Mac (see 7.13). The ~7 GB budget **includes** the ~3–4 GB model. During k6 load tests and chaos drills, the full stack runs; Phoenix may be switched off if memory pressure appears. **Fallback** if vllm-metal can't serve the chosen model: `mlx-lm`'s OpenAI-compatible server locally; real vLLM remains mandatory for the Kaggle benchmark.
5. **CI gate model on cache miss:** a small Groq-hosted model in the worker role via a repository secret (solo repo, so fork-PR secret limits don't apply). The gate measures the pipeline and prompts, not the exact local worker; this is stated in the README.
6. **Target contract is given**, not inferred: the user picks the target table in the UI (or the S3 prefix `landing/<table>/` names it). Contract inference is a non-goal.
7. **Quarantine levels and scoring:**
   - *Cell*: unrecoverable value → set null + `_flags` column entry. Flagging a truly unrecoverable cell counts as **correct**; flagging a recoverable cell counts as **incorrect** (accuracy loss, not fabrication); outputting a value for an unrecoverable cell counts as **fabrication**.
   - *Row*: rows that fail the contract after repair go to the quarantine table; counted as missing for row recall.
   - *File*: if caps are exhausted, the whole file is quarantined; all its rows count as missing.
   - The drift generator's expected output encodes these three outcomes explicitly.
8. **Kubernetes:** Helm charts + KEDA config are deliverables that must deploy on kind; one recorded demo on a reduced stack shows KEDA scaling capped by the rate-limit budget. Not part of the everyday dev loop.
9. **NYC TLC ground truth:** schema-level (column mapping, types, units) per year from the official data dictionaries, plus cell-level checks where the mapping is deterministic (renames, type casts). No human labelling.

## 12. Left to the builder

Exact package versions (latest stable, verified); repo layout and module boundaries; DB schema; API route shapes; prompt wording; Pandera contract details per table; drift recipe parameters and case mix; CI slice size and regression thresholds (must be above the measured noise floor); Grafana panel design; Helm chart structure; which Groq model is used (from what's currently offered).
