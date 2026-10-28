import { EVAL_SUMMARY } from "./fixtures/eval-summary";
import { generatePreview } from "./fixtures/preview";
import {
  RUN_CLEAN,
  RUN_CLEAN_EVENTS,
  RUN_CLEAN_TRANSFORMS,
} from "./fixtures/run-clean";
import {
  RUN_QUARANTINED,
  RUN_QUARANTINED_EVENTS,
  RUN_QUARANTINED_TRANSFORMS,
} from "./fixtures/run-quarantined";
import {
  RUN_REPAIRED,
  RUN_REPAIRED_EVENTS,
  RUN_REPAIRED_TRANSFORMS,
} from "./fixtures/run-repaired";
import type {
  ApiClient,
  DataPreview,
  EvalSummary,
  RunEvent,
  RunSummary,
  TableName,
  Transform,
} from "./types";

/** Default pause between replayed events, so the live timeline is watchable. */
export const MOCK_EVENT_DELAY_MS = 400;

export interface MockApiClientOptions {
  /** Delay between streamed events in ms (default {@link MOCK_EVENT_DELAY_MS}). */
  delayMs?: number;
}

interface MockRun {
  summary: RunSummary;
  events: readonly RunEvent[];
  transforms: readonly Transform[];
  /** Which fixture's preview generator backs this run. */
  previewOf: string;
}

const FIXTURE_RUNS: readonly MockRun[] = [
  {
    summary: RUN_REPAIRED,
    events: RUN_REPAIRED_EVENTS,
    transforms: RUN_REPAIRED_TRANSFORMS,
    previewOf: RUN_REPAIRED.id,
  },
  {
    summary: RUN_QUARANTINED,
    events: RUN_QUARANTINED_EVENTS,
    transforms: RUN_QUARANTINED_TRANSFORMS,
    previewOf: RUN_QUARANTINED.id,
  },
  {
    summary: RUN_CLEAN,
    events: RUN_CLEAN_EVENTS,
    transforms: RUN_CLEAN_TRANSFORMS,
    previewOf: RUN_CLEAN.id,
  },
];

/** Resolves after `ms`, or early (without rejecting) when `signal` aborts. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}

/** Re-stamps run-repaired's log as a brand-new run starting at `createdAt`. */
function replayAs(summary: RunSummary): RunEvent[] {
  const origin = Date.parse(RUN_REPAIRED_EVENTS[0]?.at ?? summary.createdAt);
  const start = Date.parse(summary.createdAt);
  return RUN_REPAIRED_EVENTS.map((e) => {
    const at = new Date(start + (Date.parse(e.at) - origin)).toISOString();
    if (e.type === "run_started") {
      return {
        ...e,
        at,
        runId: summary.id,
        fileName: summary.fileName,
        table: summary.table,
      };
    }
    return { ...e, at };
  });
}

/**
 * In-memory {@link ApiClient} backed by fixtures, so every screen works with
 * no backend. Safe to construct on the server or the client. Each instance
 * keeps its own created runs and `run-<n>` counter.
 */
export class MockApiClient implements ApiClient {
  readonly isDemo = true;
  readonly #delayMs: number;
  readonly #runs = new Map<string, MockRun>();
  #nextRun = 1;

  constructor(opts: MockApiClientOptions = {}) {
    this.#delayMs = Math.max(0, opts.delayMs ?? MOCK_EVENT_DELAY_MS);
    for (const run of FIXTURE_RUNS) this.#runs.set(run.summary.id, { ...run });
  }

  async listRuns(): Promise<RunSummary[]> {
    return [...this.#runs.values()]
      .map((r) => ({ ...r.summary }))
      .sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  }

  async createRun(file: File, table: TableName): Promise<RunSummary> {
    const summary: RunSummary = {
      id: `run-${this.#nextRun++}`,
      fileName: file.name,
      table,
      rows: RUN_REPAIRED.rows,
      status: "queued",
      createdAt: new Date().toISOString(),
      costUsd: 0,
    };
    this.#runs.set(summary.id, {
      summary,
      events: replayAs(summary),
      transforms: RUN_REPAIRED_TRANSFORMS,
      previewOf: RUN_REPAIRED.id,
    });
    return { ...summary };
  }

  async streamEvents(
    runId: string,
    onEvent: (e: RunEvent) => void,
    signal: AbortSignal,
  ): Promise<void> {
    const run = this.#get(runId);
    for (const [i, event] of run.events.entries()) {
      if (i > 0 && this.#delayMs > 0) await pause(this.#delayMs, signal);
      if (signal.aborted) return;
      this.#track(run, event);
      onEvent(structuredClone(event));
    }
  }

  async getTransforms(runId: string): Promise<Transform[]> {
    return this.#get(runId).transforms.map((t) => ({ ...t }));
  }

  async getPreview(runId: string, limit: number): Promise<DataPreview> {
    const run = this.#get(runId);
    return generatePreview(run.previewOf, run.summary.rows, limit);
  }

  async getEvalSummary(): Promise<EvalSummary> {
    return structuredClone(EVAL_SUMMARY);
  }

  #get(runId: string): MockRun {
    const run = this.#runs.get(runId);
    if (!run) throw new Error(`Unknown run: ${runId}`);
    return run;
  }

  /** Keeps a created run's summary in step with what has been streamed. */
  #track(run: MockRun, event: RunEvent): void {
    if (event.type === "run_started" && run.summary.status === "queued") {
      run.summary = { ...run.summary, status: "running" };
    } else if (event.type === "run_finished") {
      run.summary = {
        ...run.summary,
        status: event.status,
        costUsd: event.costUsd,
      };
    }
  }
}
