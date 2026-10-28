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
