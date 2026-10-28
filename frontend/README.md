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

```bash
NEXT_PUBLIC_SLUICE_API_URL=http://localhost:8080 bun run dev
```

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

| Command             | What it does                         |
| ------------------- | ------------------------------------ |
| `bun run lint`      | Biome (lint + format check) + Oxlint |
| `bun run format`    | Biome format, writes changes         |
| `bun run typecheck` | `tsgo --noEmit`                      |
| `bun run test`      | Vitest + Testing Library (jsdom)     |
| `bun run build`     | Production build                     |
| `bun run e2e`       | Playwright (+ axe)                   |

`bun run e2e` builds the app into `.next-e2e/` (so it never touches a running
`next dev`'s `.next/`) and serves it on port 3200 (`E2E_PORT` overrides). It
covers the upload → live run → Code/Plan flow, axe (WCAG 2.2 AA, light and
dark), keyboard order, phone-width layout and reduced motion/transparency.
Locally it drives your installed Google Chrome; CI installs Playwright's
Chromium. Failures and the run screenshots land in `test-results/`.

`tsconfig.json` lists `.next-e2e/types/**/*.ts` and `.next-e2e/dev/types/**/*.ts`
in `include` and `.next-e2e` in `exclude` on purpose. `next build` with a custom
`distDir` checks for those include strings and, if they are missing, adds them
and reformats the whole file. The exclude then keeps a possibly stale e2e build's
global route types (`PageProps`, `LayoutProps`) out of `bun run typecheck`, so
only `.next/types` is type-checked.
