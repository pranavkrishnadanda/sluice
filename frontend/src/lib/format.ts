import type { CI } from "@/lib/api/types";

/* Formatters are created once: Intl constructors are comparatively costly. */
const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const USD_SMALL = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumSignificantDigits: 2,
});
const COUNT = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const SECONDS = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

/**
 * US dollars. Exactly zero is "$0"; sub-cent amounts keep two significant
 * digits ("$0.0031") so per-run LLM costs stay readable; otherwise cents.
 */
export function formatUsd(n: number): string {
  if (n === 0) return "$0";
  return Math.abs(n) < 0.01 ? USD_SMALL.format(n) : USD.format(n);
}

/** Integer count with Indian digit grouping: 124500 -> "1,24,500". */
export function formatCount(n: number): string {
  return COUNT.format(n);
}

/** Durations: under a second in ms ("820 ms"), otherwise seconds ("1.4 s"). */
export function formatMs(ms: number): string {
  if (Math.abs(ms) < 1_000) return `${Math.round(ms)} ms`;
  return `${SECONDS.format(ms / 1_000)} s`;
}

function pctNumber(x: number): string {
  return (x * 100).toFixed(1);
}

/** Fraction as a one-decimal percentage: 0.814 -> "81.4%". */
export function formatPct(x: number): string {
  return `${pctNumber(x)}%`;
}

export type CIUnit = "pct" | "usd" | "ms";

/** A CI's mean and its bracketed bounds, formatted separately. */
export function formatCIParts(
  ci: CI,
  unit: CIUnit = "pct",
): { mean: string; interval: string } {
  if (unit === "usd") {
    return {
      mean: formatUsd(ci.mean),
      interval: `[${formatUsd(ci.lo)}, ${formatUsd(ci.hi)}]`,
    };
  }
  if (unit === "ms") {
    return {
      mean: formatMs(ci.mean),
      interval: `[${formatMs(ci.lo)}, ${formatMs(ci.hi)}]`,
    };
  }
  return {
    mean: formatPct(ci.mean),
    interval: `[${pctNumber(ci.lo)}, ${pctNumber(ci.hi)}]`,
  };
}

/**
 * Mean with its interval: "81.4% [79.0, 83.6]" for fractions,
 * "$0.12 [$0.10, $0.15]" with `unit: "usd"`, or "1.1 s [940 ms, 1.3 s]"
 * with `unit: "ms"`. `null` (not measured) is "—".
 */
export function formatCI(ci: CI | null, unit: CIUnit = "pct"): string {
  if (!ci) return "—";
  const { mean, interval } = formatCIParts(ci, unit);
  return `${mean} ${interval}`;
}
