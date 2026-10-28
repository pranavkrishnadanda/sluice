# Sluice v1 · UI shell (liquid glass) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Sluice web UI (spec §7.12) against a fixed, documented API contract served by a mock client, so the UI is complete and testable before the real backend (v1 plan 2) exists; swapping to the real API later is a config change.

**Architecture:** A Next.js App Router app in `frontend/`. All data goes through one `ApiClient` interface with two implementations: `MockApiClient` (fixtures + timed replay of run events, used now) and `HttpApiClient` (REST + SSE, wired in v1 plan 2). The contract (TypeScript types + `docs/api/contract.md`) is the hand-off point: plan 2's FastAPI models must mirror it. The liquid-glass look is built from CSS design tokens with light/dark themes.

**Tech Stack:** Next.js (App Router), React, TypeScript (strict), Tailwind CSS, shadcn/ui (restyled), Motion, TanStack Table + TanStack Virtual, Shiki, Apache ECharts; Bun, Biome, Oxlint, tsgo, Vitest + Testing Library, Playwright (+ axe).

**Spec:** `docs/superpowers/specs/2026-10-28-sluice-design.md` (§7.11 API, §7.12 Web UI, §9 v1 item 9; mockup `docs/design/ui-mockup-liquid-glass.html`).

## Global Constraints

- Package manager **Bun** only (`bun add`, `bun run`, `bunx`). No npm/yarn/pnpm lockfiles. Commit `bun.lock`.
- Latest stable of every dependency, **verified at Task 1** from the npm registry (seen 2026-10-28: next 16.4.3, react 19.3.1, tailwindcss 4.3.3, @biomejs/biome 2.6.3, oxlint 1.90.0, vitest 5.0.5, @playwright/test 1.64.1, echarts 6.1.0, motion 14.7.1, shiki 4.5.0, @tanstack/react-table 9.3.1). Re-check; don't pin from memory.
- Lint/format with **Biome** + **Oxlint**; type-check with **tsgo** (`@typescript/native-preview`). TypeScript `strict: true`, no `any`.
- All code lives under `frontend/`. **Do not touch `backend/`, the root `Makefile` or `.github/workflows/ci.yml`** (a parallel backend branch owns them). Frontend CI goes in its own `.github/workflows/frontend.yml`.
- Dev server port **3000**. Never use host ports 5432 or 8000.
- **Identity:** product name "Sluice". No ChatGPT/Claude/OpenAI/Anthropic names, logos or brand colours anywhere in the UI.
- **Honesty:** whenever the mock client is active, a visible **"Demo data"** badge is shown in the header and on every number-bearing view. Fixture numbers are illustrative and must never be presented as measured results.
- Accessibility: text contrast WCAG AA in both themes; honour `prefers-reduced-motion` and `prefers-reduced-transparency` (solid panels instead of glass); full keyboard navigation.
- Commits: owner is sole author; **no `Co-Authored-By` or any AI attribution**; Conventional Commits.
- Never read, print or commit `.env*` (only `frontend/.env.example` may be committed).

## Review Focus

1. **Event stream ends mid-run or repeats events** (network drop, duplicate `seq`) → the thread shows the last known state with a "Connection lost" + Retry control; events whose `seq` was already applied are ignored; no crash. Pinned in Task 3 and Task 5.
2. **Run with zero LLM nodes** (rules fixed everything; `plan_created.nodes = []`) → the step list shows "No LLM work needed", the Plan tab shows an empty state, cost shows $0. Pinned in Task 5 and Task 6.
3. **Large preview** (50,000 rows) → the Data tab stays responsive (virtualised; fewer than 200 row elements in the DOM). Pinned in Task 6.
4. **Reduced transparency / reduced motion** → glass panels render solid and animations are disabled. Pinned in Task 2 and Task 8.
5. **Narrow viewport (≤ 768 px)** → sidebar collapses behind a menu button, side panel becomes a drawer, no horizontal page scroll. Pinned in Task 4 and Task 8.

---

## File Structure

```
frontend/
  package.json · bun.lock · tsconfig.json · next.config.ts · biome.json · .oxlintrc.json
  vitest.config.ts · playwright.config.ts · .env.example
  src/app/
    layout.tsx                  # fonts, ThemeProvider, ApiProvider
    page.tsx                    # redirect -> /runs
    runs/page.tsx               # empty state + composer
    runs/[id]/page.tsx          # run thread + side panel
    evals/page.tsx              # results table + cost–quality chart
    globals.css                 # design tokens, glass utilities, themes
  src/lib/api/
    types.ts                    # THE contract (mirrors docs/api/contract.md)
    client.ts                   # ApiClient interface + getApiClient()
    mock-client.ts              # MockApiClient
    http-client.ts              # HttpApiClient (REST + SSE)
    fixtures/                   # runs, events, transforms, evals (JSON)
  src/lib/run-state.ts          # reduceRunEvents() -> RunViewState
  src/lib/format.ts             # number / money / duration formatting
  src/lib/diff.ts               # lineDiff()
  src/components/
    api/ApiProvider.tsx
    glass/Glass.tsx
    theme/ThemeProvider.tsx · ThemeToggle.tsx
    shell/AppShell.tsx · Sidebar.tsx · DemoBadge.tsx
    thread/RunThread.tsx · StepList.tsx · Composer.tsx · FileChip.tsx
    panel/SidePanel.tsx · DataTab.tsx · CodeTab.tsx · PlanTab.tsx
    evals/ResultsTable.tsx · ParetoChart.tsx
  tests/ (Vitest, mirrors src) · e2e/ (Playwright)
docs/api/contract.md
.github/workflows/frontend.yml
```

---

### Task 1: Scaffold the Next.js app, tooling and frontend CI

**Files:**
- Create: the `frontend/` config files in the first line of File Structure, `src/app/layout.tsx`, `src/app/page.tsx`, `tests/smoke.test.tsx`, `.github/workflows/frontend.yml`
- Modify: root `.gitignore` (append `frontend/node_modules/`, `frontend/.next/`, `frontend/test-results/`, `frontend/playwright-report/`)

**Interfaces:**
- Produces: `frontend/package.json` scripts: `dev` (port 3000), `build`, `start`, `lint` (`biome check . && oxlint`), `format` (`biome format --write .`), `typecheck` (`tsgo --noEmit`), `test` (`vitest run`), `e2e` (`playwright test`).

- [ ] **Step 1:** Re-verify latest versions; scaffold with `bunx create-next-app@latest frontend` (TypeScript, App Router, Tailwind, `src/` dir, no ESLint, alias `@/*`); remove any ESLint config. Add Biome, Oxlint, `@typescript/native-preview`, Vitest, `@vitejs/plugin-react`, jsdom, `@testing-library/react`, `@testing-library/jest-dom`, `@testing-library/user-event`, Playwright, `@axe-core/playwright`.
- [ ] **Step 2: Write the failing test** `tests/smoke.test.tsx`: render the home content and assert a heading "Sluice" is in the document.
- [ ] **Step 3:** `bun run test` → FAIL; render the heading; → PASS.
- [ ] **Step 4:** `frontend.yml`: on push/PR with `paths: ["frontend/**", ".github/workflows/frontend.yml"]`; ubuntu-latest; `permissions: contents: read`; checkout → `oven-sh/setup-bun` (latest major, verified) → `bun install --frozen-lockfile` → `lint` → `typecheck` → `test` → `build` (working-directory `frontend`). Playwright joins in Task 8.
- [ ] **Step 5:** `bun run lint && bun run typecheck && bun run test && bun run build` → all pass.
- [ ] **Step 6: Commit** `chore(frontend): scaffold Next.js app with Bun, Biome, Oxlint, tsgo and Vitest`

---

### Task 2: Design tokens, glass primitive and theme toggle

**Files:**
- Create: `src/app/globals.css` (replace scaffold), `src/components/glass/Glass.tsx`, `src/components/theme/ThemeProvider.tsx`, `src/components/theme/ThemeToggle.tsx`, tests `tests/components/glass.test.tsx`, `tests/components/theme.test.tsx`
- Modify: `src/app/layout.tsx`

**Interfaces:**
- Produces:
  - CSS tokens on `:root` (light) and `[data-theme="dark"]`, plus `@media (prefers-color-scheme: dark)` for `[data-theme="system"]`: `--bg`, `--blob-1`, `--blob-2`, `--blob-3`, `--glass-bg`, `--glass-solid`, `--glass-border`, `--glass-shadow`, `--glass-highlight`, `--text`, `--text-muted`, `--accent`, `--ok`, `--warn`, `--error`, `--chip-bg`. Values come from the `.light` / `.dark` rules in `docs/design/ui-mockup-liquid-glass.html`, adjusted only where needed for WCAG AA contrast.
  - Class `.glass`: `background: var(--glass-bg); backdrop-filter: blur(28px) saturate(170%)`, 1 px gradient highlight border, `box-shadow: var(--glass-shadow)`. Under `@media (prefers-reduced-transparency: reduce)` and under `html[data-reduce-transparency]`: `background: var(--glass-solid); backdrop-filter: none`. Under `prefers-reduced-motion: reduce`: transitions/animations disabled.
  - `<Glass as?: ElementType className? children>` renders with class `glass`.
  - `ThemeProvider`: mode `"system" | "light" | "dark"`, stored in `localStorage["sluice-theme"]` (all access in try/catch), sets `data-theme` on `<html>`, default `"system"`, no flash of wrong theme (inline script in `layout.tsx`). Also sets `html[data-reduce-transparency]` from `matchMedia("(prefers-reduced-transparency: reduce)")` (test hook for Task 8).
  - `<ThemeToggle />`: button cycling system → light → dark; `aria-label` "Theme: system" / "Theme: light" / "Theme: dark".

- [ ] **Step 1: Write failing tests:**

```tsx
test("Glass renders children inside a .glass element")      // getByText("x").closest(".glass") not null
test("ThemeToggle cycles system -> light -> dark")         // aria-label changes; html[data-theme] "light" then "dark"
test("theme persists to localStorage")                     // localStorage["sluice-theme"] === "dark" after two clicks
test("theme survives localStorage throwing")               // setItem throws -> no crash; data-theme still updates
test("reduced transparency sets html data attribute")      // matchMedia mocked true -> html has data-reduce-transparency
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS; `lint`, `typecheck` pass.
- [ ] **Step 5: Commit** `feat(frontend): add liquid-glass design tokens, Glass primitive and theme toggle`

---

### Task 3: API contract, mock client and run-state reducer

**Files:**
- Create: `src/lib/api/types.ts`, `client.ts`, `mock-client.ts`, `http-client.ts`, `src/lib/api/fixtures/*`, `src/lib/run-state.ts`, `src/lib/format.ts`, `src/components/api/ApiProvider.tsx`, `docs/api/contract.md`, `frontend/.env.example`, tests `tests/lib/mock-client.test.ts`, `tests/lib/run-state.test.ts`, `tests/lib/format.test.ts`, `tests/lib/http-client.test.ts`

**Interfaces (exact — this is the hand-off contract to the backend):**

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
  | { type: "attempt_failed"; nodeId: string; attempt: number; maxAttempts: number; error: string }
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

- HTTP mapping (written into `docs/api/contract.md`; implemented by `HttpApiClient`; base URL `NEXT_PUBLIC_SLUICE_API_URL`): `GET /runs`, `POST /runs` (multipart `file`, `table`), `GET /runs/{id}/events` (SSE; each message `id: <seq>` + `data: <RunEvent JSON>`; reconnect sends `Last-Event-ID`), `GET /runs/{id}/transforms`, `GET /runs/{id}/preview?limit=`, `GET /evals/summary`. JSON is camelCase; the backend's Pydantic models must mirror these names.
- `getApiClient(): ApiClient` — `MockApiClient` when `NEXT_PUBLIC_SLUICE_API_URL` is unset or `"mock"`, else `HttpApiClient`. `ApiProvider` puts it in React context; `useApi()` reads it.
- `MockApiClient(opts?: { delayMs?: number })`, `isDemo = true`, `MOCK_EVENT_DELAY_MS = 400` default. Fixtures for three runs:
  - `run-clean`: orders, easy (whitespace + dmy dates); rules fix everything; `plan_created.nodes = []`; succeeded; cost 0.
  - `run-repaired`: `orders_2026-10.csv`, 48,210 rows; issues: rename `amt`→`o_totalprice`, Indian number strings, paise; plan with 2 nodes (second depends on first); first node fails attempt 1 ("contract check failed: 14 rows in paise") and passes attempt 2; verified 12/12; loaded 48,210; succeeded. Two transforms for the failing node (attempt 1 and 2) and one for the other.
  - `run-quarantined`: lineitem; broken FKs; loaded with `rowsQuarantined: 212`; status `quarantined`.
  - `streamEvents` replays the run's events with `delayMs` between them and returns early on `signal.abort()`. `createRun` returns id `run-<n>` (counter) that replays `run-repaired`'s events with the new id/file name. `getPreview("run-repaired", limit)` returns `min(limit, 50_000)` deterministic generated rows (built on demand). `getEvalSummary` returns a fixture with 3 systems (rules, always-strong, sluice) and a Pareto fixture (3 baseline points, one router line of 5 λ points).
- `RunViewState` and `reduceRunEvents(state: RunViewState | undefined, e: RunEvent): RunViewState` — pure; ignores any event with `seq <= state.lastSeq`; tracks `status`, `issues`, `ruleActions`, `nodes` (each with `status: "pending" | "running" | "retrying" | "done" | "failed"`, `attempt`, `maxAttempts`, `tier`, `costUsd`, `latencyMs`), `noLlmWork` (true when the plan has 0 nodes), `verified`, `loaded`, totals, `summary`, `finished`.
- `format.ts`: `formatUsd(n)` (`0` → "$0", `0.0031` → "$0.0031", `1.5` → "$1.50"), `formatCount(n)` (en-IN grouping: 124500 → "1,24,500", 48210 → "48,210"), `formatMs(ms)` (820 → "820 ms", 1400 → "1.4 s"), `formatPct(x)` (0.814 → "81.4%"), `formatCI(ci)` ("81.4% [79.0, 83.6]"; `null` → "—").

- [ ] **Step 1: Write failing tests:**

```ts
test("mock streams run-repaired events in seq order ending with run_finished")
test("mock stream stops on abort")                      // abort after 2 events -> exactly 2 received
test("reducer ignores duplicate and stale seq")         // apply seq 3 twice -> state deep-equal
test("reducer tracks retry then success")              // node: retrying (attempt 2/3) -> done
test("reducer marks noLlmWork for empty plan")
test("getApiClient returns mock when env unset")       // isDemo === true
test("formatCount uses Indian grouping")               // 124500 -> "1,24,500"; 48210 -> "48,210"
test("formatUsd / formatMs / formatPct / formatCI")    // values listed above
test("HttpApiClient.listRuns calls GET {base}/runs")   // stubbed fetch
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement, including `docs/api/contract.md` (types verbatim + HTTP/SSE mapping + camelCase note) and `.env.example` (`NEXT_PUBLIC_SLUICE_API_URL=mock`). **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(frontend): add API contract, mock client and run-state reducer`

---

### Task 4: App shell — sidebar, header, demo badge, responsive layout

**Files:**
- Create: `src/components/shell/AppShell.tsx`, `Sidebar.tsx`, `DemoBadge.tsx`, `src/app/runs/page.tsx`, tests `tests/components/shell.test.tsx`
- Modify: `src/app/layout.tsx`, `src/app/page.tsx` (redirect to `/runs`)

**Interfaces:**
- Consumes: `useApi()`, `RunSummary`, `Glass`, `ThemeToggle`.
- Produces: `<AppShell sidePanel?: ReactNode>{children}</AppShell>` — glass sidebar (~240 px), centred main column (max ~760 px), optional glass right panel (~380 px), all over a blurred colour-blob backdrop (`--blob-*`). Header: "Sluice", `<DemoBadge />` (renders only when `api.isDemo`), `<ThemeToggle />`.
- Sidebar: "New pipeline run" (→ `/runs`), "Today" runs from `listRuns()` (status dot + file name, link `/runs/[id]`, `aria-current` on the active run), "Evals" link (→ `/evals`); loading skeleton; empty state "No runs yet".
- Below 1024 px the right panel is a toggleable drawer (button "Details"); below 768 px the sidebar hides behind a menu button (`aria-expanded`).

- [ ] **Step 1: Write failing tests:** sidebar lists the 3 fixture runs; the active run link has `aria-current="page"`; DemoBadge shows with the mock client and is absent with a stub client whose `isDemo = false`; at 600 px width a menu button exists and toggles the sidebar's visibility.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(frontend): add glass app shell, sidebar and demo badge`

---

### Task 5: Run thread — live step list, summary and composer

**Files:**
- Create: `src/components/thread/RunThread.tsx`, `StepList.tsx`, `Composer.tsx`, `FileChip.tsx`, `src/app/runs/[id]/page.tsx`, tests `tests/components/thread.test.tsx`
- Modify: `src/app/runs/page.tsx` (composer in the empty state)

**Interfaces:**
- Consumes: `useApi()`, `reduceRunEvents`, `RunViewState`, `format.ts`, `AppShell`.
- Produces:
  - `<RunThread runId: string />` — user bubble with `<FileChip name rows />` ("orders_2026-10.csv · 48,210 rows") and "Load this into the {table} table."; then `<StepList state />` with one row per milestone in order: **Profiled** ("{n} issues found"), **Rules** ("{n} fixes", chip "rules · $0"), one row per plan node (title; chip = tier; when done: `formatUsd(cost) · formatMs(latency)`; when retrying: ↻ and "attempt {n}/{max}"; when the plan is empty a single row "No LLM work needed"), **Verified** ("{passed}/{total} contract checks"), **Loaded** ("Loaded {formatCount(rows)} rows", plus "{n} quarantined" when > 0). The in-flight row shows a pulsing dot (static under reduced motion). After `run_finished`, an assistant message with `summary` and total cost. If `streamEvents` resolves without `run_finished`, show "Connection lost" with a Retry button that re-subscribes (the reducer de-duplicates by `seq`).
  - `<Composer onSubmit(file: File, table: TableName) />` — glass input bar: file picker + drag-and-drop zone, table select (`orders` | `lineitem`), submit; accepts `.csv`, `.json`, `.parquet` only (others show an inline error and don't submit). On the `/runs` page it calls `api.createRun` and navigates to `/runs/{id}`.
  - Rows animate in with Motion (fade + 4 px rise, ≤ 200 ms), disabled under reduced motion.

- [ ] **Step 1: Write failing tests** (mock client, `delayMs: 0`):

```tsx
test("run-repaired shows retry then success")       // "attempt 2/3" appears; final "Loaded 48,210 rows"
test("run-clean shows no LLM work")                 // "No LLM work needed"; "$0"
test("run-quarantined shows quarantined count")     // "212 quarantined"
test("stream drop shows Connection lost and Retry resumes")  // stub ends after 3 events; Retry -> finished
test("composer rejects .xlsx")                      // inline error; createRun not called
test("composer submits and navigates")              // createRun(file, "orders"); router.push("/runs/run-1")
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(frontend): add live run thread, step list and composer`

---

### Task 6: Side panel — Data, Code and Plan tabs

**Files:**
- Create: `src/components/panel/SidePanel.tsx`, `DataTab.tsx`, `CodeTab.tsx`, `PlanTab.tsx`, `src/lib/diff.ts`, tests `tests/components/panel.test.tsx`, `tests/lib/diff.test.ts`
- Modify: `src/app/runs/[id]/page.tsx` (pass `<SidePanel runId state />` to `AppShell`)

**Interfaces:**
- Consumes: `useApi()` (`getPreview(runId, 50_000)`, `getTransforms(runId)`), `RunViewState`, Shiki, TanStack Table + TanStack Virtual, shadcn/Radix Tabs.
- Produces:
  - `<SidePanel runId state />` with tabs **Data · Code · Plan** (keyboard-accessible).
  - **Data:** virtualised table with columns `pk | column | before | after`; changed `before` values struck through; flagged cells show a "flagged" pill; toggle "Changed only".
  - **Code:** transforms grouped by node with an attempt selector; Shiki highlighting (Python grammar for `polars`, SQL for `sql`), theme follows light/dark; when a node has ≥ 2 attempts, a "Diff vs previous attempt" toggle shows a unified line diff from `lineDiff(a: string, b: string): { kind: "same" | "add" | "del"; text: string }[]` (LCS over lines). Shiki is loaded with a dynamic import (not in the initial bundle).
  - **Plan:** inline-SVG DAG — nodes placed in layers by dependency depth (`data-layer` attribute = depth), curved edges, fill by node status, tier label; empty plan → "Rules fixed everything — no plan needed".

- [ ] **Step 1: Write failing tests:**

```tsx
test("data tab virtualises 50k rows")         // querySelectorAll('[role="row"]').length < 200
test("flagged cell shows pill")
test("code tab shows attempts and diff")      // run-repaired failing node: diff has >= 1 "add" and >= 1 "del" line
test("lineDiff basic")                        // lineDiff("a\nb", "a\nc") -> same "a", del "b", add "c"
test("plan tab layers nodes by dependency")   // dependent node has data-layer="1", root has "0"
test("plan tab empty state")                  // run-clean -> "Rules fixed everything"
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(frontend): add data, code and plan side-panel tabs`

---

### Task 7: Evals page — results table and cost–quality chart

**Files:**
- Create: `src/app/evals/page.tsx`, `src/components/evals/ResultsTable.tsx`, `ParetoChart.tsx`, tests `tests/components/evals.test.tsx`

**Interfaces:**
- Consumes: `useApi().getEvalSummary()`, `CI`, `format.ts` (`formatCI`), ECharts (dynamic import, client-only).
- Produces: `<ResultsTable summary />` — one row per system: cell accuracy, fabrication rate, pass^4, cost per 1k rows; caption "{split} · n = {n}". `<ParetoChart summary />` — x = cost per 1k rows (log axis), y = quality; baselines as labelled points, routers as lines (λ in tooltip); colours from CSS tokens; re-renders on theme change; resizes via ResizeObserver; a visually hidden `<table>` lists every point for screen readers. The page shows `DemoBadge` with the mock client.

- [ ] **Step 1: Write failing tests:** a fixture system renders `formatCI` output with brackets; a `null` metric renders "—"; the hidden table has one row per fixture point (3 baseline + 5 router = 8); the option passed to a mocked `setOption` has `xAxis.type === "log"`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(frontend): add evals page with results table and cost–quality chart`

---

### Task 8: End-to-end, accessibility and responsive checks

**Files:**
- Create: `e2e/run-flow.spec.ts`, `e2e/a11y.spec.ts`, `e2e/responsive.spec.ts`
- Modify: `playwright.config.ts` (webServer: `bun run build && bun run start -p 3000`), `.github/workflows/frontend.yml` (add Playwright job: `bunx playwright install --with-deps chromium`, run e2e, upload `playwright-report/` and `test-results/` on failure)

**Interfaces:** none new.

- [ ] **Step 1: Write the specs:**
  - `run-flow`: `/runs` → upload a small CSV via the composer → lands on `/runs/run-1` → waits for "Loaded" → Code tab → toggle diff → Plan tab shows 2 nodes.
  - `a11y`: axe on `/runs/run-repaired` and `/evals` in light and dark → zero serious/critical violations; keyboard-only Tab order reaches composer, tabs and theme toggle.
  - `responsive`: at 390×844 `document.documentElement.scrollWidth <= window.innerWidth`, menu button visible, "Details" opens the drawer; with `reducedMotion: "reduce"` and `html[data-reduce-transparency]` set, `.glass` computed `backdrop-filter` is `none`.
  - Full-page screenshots of `/runs/run-repaired` in light and dark saved to `test-results/` (CI artifacts; not committed).
- [ ] **Step 2:** `bun run e2e` → fix until green.
- [ ] **Step 3:** `bun run lint && bun run typecheck && bun run test && bun run build` → pass.
- [ ] **Step 4: Commit** `test(frontend): add Playwright e2e, accessibility and responsive checks`

---

## Done when

- `lint`, `typecheck`, `test`, `build` and `e2e` pass locally and in `frontend.yml`.
- With no env set the app runs on the mock client, shows the "Demo data" badge, and matches mockup layout A in light and dark liquid glass.
- `docs/api/contract.md` fully specifies the types and HTTP/SSE mapping that v1 plan 2's backend must implement.
