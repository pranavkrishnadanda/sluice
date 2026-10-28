import type { EvalSummary } from "../types";

/**
 * Illustrative demo numbers only (the UI labels them "Demo data"). The
 * `sluice` system row is the router's default operating point, lambda = 0.5
 * (its cell accuracy and cost equal that Pareto point's quality and cost).
 */
export const EVAL_SUMMARY: EvalSummary = {
  split: "holdout",
  n: 400,
  generatedAt: "2026-10-27T21:05:00.000Z",
  systems: [
    {
      system: "rules",
      cellAccuracy: { mean: 0.712, lo: 0.69, hi: 0.733 },
      fabricationRate: { mean: 0, lo: 0, hi: 0.009 },
      contractPassRate: { mean: 0.618, lo: 0.57, hi: 0.664 },
      passHat4: null,
      p95LatencyMs: { mean: 210, lo: 180, hi: 245 },
      costPer1kRowsUsd: { mean: 0, lo: 0, hi: 0 },
    },
    {
      system: "always-strong",
      cellAccuracy: { mean: 0.874, lo: 0.856, hi: 0.891 },
      fabricationRate: { mean: 0.021, lo: 0.014, hi: 0.03 },
      contractPassRate: { mean: 0.968, lo: 0.948, hi: 0.981 },
      passHat4: { mean: 0.78, lo: 0.73, hi: 0.825 },
      p95LatencyMs: { mean: 4_380, lo: 3_960, hi: 4_870 },
      costPer1kRowsUsd: { mean: 0.94, lo: 0.88, hi: 1.01 },
    },
    {
      system: "sluice",
      cellAccuracy: { mean: 0.861, lo: 0.842, hi: 0.879 },
      fabricationRate: { mean: 0.012, lo: 0.007, hi: 0.019 },
      contractPassRate: { mean: 0.975, lo: 0.96, hi: 0.986 },
      passHat4: { mean: 0.81, lo: 0.76, hi: 0.85 },
      p95LatencyMs: { mean: 1_120, lo: 940, hi: 1_310 },
      costPer1kRowsUsd: { mean: 0.12, lo: 0.1, hi: 0.15 },
    },
  ],
  pareto: [
    {
      system: "rules",
      kind: "baseline",
      points: [{ costPer1kRowsUsd: 0, quality: 0.712, lambda: null }],
    },
    {
      system: "always-small",
      kind: "baseline",
      points: [{ costPer1kRowsUsd: 0.08, quality: 0.803, lambda: null }],
    },
    {
      system: "always-strong",
      kind: "baseline",
      points: [{ costPer1kRowsUsd: 0.94, quality: 0.874, lambda: null }],
    },
    {
      system: "sluice",
      kind: "router",
      points: [
        { costPer1kRowsUsd: 0.02, quality: 0.768, lambda: 2 },
        { costPer1kRowsUsd: 0.05, quality: 0.824, lambda: 1 },
        { costPer1kRowsUsd: 0.12, quality: 0.861, lambda: 0.5 },
        { costPer1kRowsUsd: 0.27, quality: 0.869, lambda: 0.25 },
        { costPer1kRowsUsd: 0.51, quality: 0.872, lambda: 0.1 },
      ],
    },
  ],
};
