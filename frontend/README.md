# Sluice frontend

The Sluice web UI: a Next.js App Router app (React, TypeScript strict, Tailwind CSS).

## Develop

```bash
bun install
bun run dev        # http://localhost:3000
```

No dependency install scripts are needed, so none are trusted
(`"trustedDependencies": []`): `sharp` ships prebuilt binaries through its
optional `@img/*` packages, and `bun pm untrusted` reports nothing blocked.

## Demo mode and the backend

The UI talks to the backend through one `ApiClient` interface
(`src/lib/api/`), chosen from `NEXT_PUBLIC_SLUICE_API_URL`:

| Value                               | Client                                                                  |
| ----------------------------------- | ----------------------------------------------------------------------- |
| unset, empty or `mock`              | `MockApiClient`: built-in fixture runs and eval results, in the browser |
| a URL, e.g. `http://localhost:8080` | `HttpApiClient` against the FastAPI backend                             |

**Demo mode** needs no backend. Every number-bearing view (header, run
summary, side panel, eval cards) shows a "Demo data" badge. A file you upload
is **not** uploaded or read: the mock replays a sample run and says so in the
thread.

- The backend listens on **port 8080** (8000 is reserved on the owner's
  machine); it must allow CORS from the UI's origin, including on the SSE
  stream and for the `Last-Event-ID` header. See
  [`docs/api/contract.md`](../docs/api/contract.md) for the full contract.
- `NEXT_PUBLIC_SLUICE_API_URL` is **inlined at build time**: after changing
  it, restart `bun run dev` or rebuild. A built app cannot be pointed at a
  different backend at runtime.

### Running on the real backend

```bash
# from the repo root: services, then the API on http://localhost:8080
make up && make api

# here, in another shell
NEXT_PUBLIC_SLUICE_API_URL=http://localhost:8080 bun run dev
```

The "Demo data" badges disappear and uploads go through the real pipeline
(real model calls, using the keys in `backend/.env`). After changing
`NEXT_PUBLIC_SLUICE_API_URL`, restart `bun run dev`, or rebuild
(`bun run build`) and restart `bun run start`.

The API's CORS allow-list defaults to `http://localhost:3000`,
`http://127.0.0.1:3000` and the same two on port 3100. Serving the UI on any
other origin needs that origin in `SLUICE_UI_ORIGINS` (comma-separated, no
trailing slash) when the API starts:

```bash
SLUICE_UI_ORIGINS=http://localhost:3210,http://127.0.0.1:3210 make api
```

Right after `loaded`, the preview can answer `409 not_ready` while it is
still being written; the Data tab retries it for about 16 seconds before
offering a Retry button.

## Stack choices

- **Next.js App Router, React, TypeScript strict, Tailwind CSS.** Server
  Components by default; client components only where there is interaction
  or a live stream.
- **TanStack Virtual, without TanStack Table.** The Data tab is a native
  `<table>` with a fixed row height, virtualised by `@tanstack/react-virtual`.
  It has no sorting, grouping or column state, so a table engine would add
  weight without features.
- **Radix Tabs, without shadcn/ui.** The side panel uses
  `@radix-ui/react-tabs` directly for its keyboard and ARIA behaviour. The
  rest of the UI is a small set of local components (glass surfaces, pills,
  chips) styled with Tailwind tokens, so a component kit's generated sources
  would mostly be unused.
- **ECharts** (modular imports, SVG renderer, loaded on the evals page only)
  for the cost-quality chart; **Shiki** (loaded on demand) for code
  highlighting.
- **Bun** for packages and scripts; **Biome** and **Oxlint** for lint and
  format; **tsgo** for type checking; **Vitest** + Testing Library and
  **Playwright** + axe for tests.

## Checks

| Command             | What it does                            |
| ------------------- | --------------------------------------- |
| `bun run lint`      | Biome (lint + format check) + Oxlint    |
| `bun run format`    | Biome format, writes changes            |
| `bun run typecheck` | `tsgo --noEmit`                         |
| `bun run test`      | Vitest + Testing Library (jsdom)        |
| `bun run build`     | Production build                        |
| `bun run e2e`       | Playwright (+ axe)                      |
| `bun run e2e:live`  | Playwright on the real backend (opt-in) |

`bun run e2e` builds the app into `.next-e2e/` (so it never touches a running
`next dev`'s `.next/`) and serves it on port 3200 (`E2E_PORT` overrides). It
covers the upload → live run → Code/Plan flow, axe (WCAG 2.2 AA, light and
dark), keyboard order, phone-width layout and reduced motion/transparency.
Locally it drives your installed Google Chrome; CI installs Playwright's
Chromium. Failures and the run screenshots land in `test-results/`.

### Live-backend e2e

`e2e/live-backend.spec.ts` uploads one real dev case through the UI and checks
the whole flow against a running API: the run in the sidebar, steps
streaming live (the plan is on screen before the run finishes), the status
badge, before/after rows in the Data tab, each accepted transform in the Code
tab, the Plan steps, no "Demo data" badge, no console errors and axe. It is
opt-in (skipped unless `SLUICE_E2E_API_URL` is set), never runs in
`bun run e2e` or CI, and makes real model calls.

```bash
# repo root: the API must allow the e2e UI's origin (port 3210)
make up
SLUICE_UI_ORIGINS=http://localhost:3210,http://127.0.0.1:3210 make api

# here: builds into .next-e2e-live/ against that API and serves it on 3210
SLUICE_E2E_API_URL=http://localhost:8080 bun run e2e:live
```

| Variable               | Default                | Meaning                                         |
| ---------------------- | ---------------------- | ----------------------------------------------- |
| `SLUICE_E2E_API_URL`   | unset (spec skipped)   | API base URL; also baked into the UI build      |
| `SLUICE_E2E_CASE`      | `dev-0025`             | Dev case to upload (`input.*`, `manifest.json`) |
| `SLUICE_E2E_CASES_DIR` | `../backend/cases/dev` | Where the cases are (`make cases` builds them)  |
| `E2E_LIVE_PORT`        | `3210`                 | Port for the e2e UI                             |

A file is loaded at most once: uploading the same case again is reported
"file already loaded" without running the pipeline. The spec accepts that
too (and checks the skip), so to exercise the full pipeline again pick a case
that has not been loaded (`SLUICE_E2E_CASE=dev-0050`). The full-page
screenshot lands in `test-results/screenshots/live-<case>.png`.

`tsconfig.json` lists `.next-e2e/types/**/*.ts` and `.next-e2e/dev/types/**/*.ts`
(and the same for `.next-e2e-live`) in `include` and both directories in
`exclude` on purpose. `next build` with a custom
`distDir` checks for those include strings and, if they are missing, adds them
and reformats the whole file. The exclude then keeps a possibly stale e2e build's
global route types (`PageProps`, `LayoutProps`) out of `bun run typecheck`, so
only `.next/types` is type-checked.


### Demo GIF

`scripts/record-demo.ts` records the README GIF: one real dev case through
the live UI (upload, live run, Plan, Code and Data tabs, dark then light) at
1440×900. Run it from the repo root with `make demo-gif`, which also converts
the video to `docs/media/sluice-demo.gif`. It needs the same API as the
live-backend e2e (origin 3210 allowed) and stops with a message if the API
isn't reachable. It builds and serves a UI on 3210 unless one is already
running there, and a reused UI must call the same API.

Prerequisites: ffmpeg on `PATH` (or `FFMPEG=/path/to/ffmpeg`), Playwright's
video encoder (`bunx playwright install ffmpeg`, once) and the system Google
Chrome. Without Chrome, run `bunx playwright install chromium` and set
`SLUICE_DEMO_CHANNEL=chromium`.

| Variable                | Default                 | Purpose                                       |
| ----------------------- | ----------------------- | --------------------------------------------- |
| `SLUICE_DEMO_API_URL`   | `http://localhost:8080` | API the UI is built against                   |
| `SLUICE_DEMO_CASE`      | `dev-0039`              | Dev case to upload; must not be loaded yet    |
| `SLUICE_DEMO_CASES_DIR` | `../backend/cases/dev/` | Where the cases live                          |
| `SLUICE_DEMO_PORT`      | `3210`                  | Port for the UI                               |
| `SLUICE_DEMO_CHANNEL`   | `chrome`                | Browser channel (`chromium` for Playwright's) |
