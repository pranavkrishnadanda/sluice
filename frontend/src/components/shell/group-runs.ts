import type { RunSummary } from "@/lib/api/types";

export type RunGroupLabel = "Today" | "Yesterday" | "Earlier";

export interface RunGroup {
  label: RunGroupLabel;
  runs: RunSummary[];
}

const LABELS: readonly RunGroupLabel[] = ["Today", "Yesterday", "Earlier"];
const DAY_MS = 86_400_000;

function startOfLocalDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/**
 * Buckets runs by the viewer's local calendar day relative to `now`, newest
 * first within each group. Empty groups are dropped; unparseable dates land in
 * "Earlier".
 */
export function groupRunsByDay(
  runs: readonly RunSummary[],
  now: Date,
): RunGroup[] {
  const today = startOfLocalDay(now);
  const buckets = new Map<RunGroupLabel, RunSummary[]>();
  const sorted = [...runs].sort(
    (a, b) => (Date.parse(b.createdAt) || 0) - (Date.parse(a.createdAt) || 0),
  );
  for (const run of sorted) {
    const at = Date.parse(run.createdAt);
    const label: RunGroupLabel =
      at >= today ? "Today" : at >= today - DAY_MS ? "Yesterday" : "Earlier";
    const bucket = buckets.get(label);
    if (bucket) bucket.push(run);
    else buckets.set(label, [run]);
  }
  return LABELS.flatMap((label) => {
    const group = buckets.get(label);
    return group ? [{ label, runs: group }] : [];
  });
}
