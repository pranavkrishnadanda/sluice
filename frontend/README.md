# Sluice frontend

The Sluice web UI: a Next.js App Router app (React, TypeScript strict, Tailwind CSS).

## Develop

```bash
bun install
bun run dev        # http://localhost:3000
```

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
