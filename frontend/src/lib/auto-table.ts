/**
 * Runs this browser started with the table left on Auto, so the thread can
 * say the table was matched rather than asked for. A per-viewer nicety:
 * kept in memory and, when allowed, in `sessionStorage` (survives a reload
 * of the tab); a run opened elsewhere simply reads as a named-table run.
 */
const KEY = "sluice.autoTableRuns";
const MAX = 50;
const runs = new Set<string>();

function stored(): string[] {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(KEY) ?? "[]");
    return Array.isArray(value)
      ? value.filter((v): v is string => typeof v === "string")
      : [];
  } catch {
    return [];
  }
}

export function rememberAutoTable(runId: string): void {
  runs.add(runId);
  try {
    const next = [...stored().filter((id) => id !== runId), runId].slice(-MAX);
    sessionStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // Storage blocked or full: the in-memory set still covers this visit.
  }
}

export function isAutoTable(runId: string): boolean {
  return runs.has(runId) || stored().includes(runId);
}
