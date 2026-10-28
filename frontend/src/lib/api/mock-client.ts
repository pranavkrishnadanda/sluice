import { ApiError } from "./errors";
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
  PreviewPage,
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
  /** Created through {@link MockApiClient.createRun}: replays a fixture. */
  sample: boolean;
  /** `loaded` has been streamed (fixture runs are complete from the start). */
  loaded: boolean;
}

/** `listRuns` returns at most this many runs, like the server. */
const MAX_LISTED_RUNS = 100;

const FIXTURE_RUNS: readonly MockRun[] = [
  {
    summary: RUN_REPAIRED,
    events: RUN_REPAIRED_EVENTS,
    transforms: RUN_REPAIRED_TRANSFORMS,
    previewOf: RUN_REPAIRED.id,
    sample: false,
    loaded: true,
  },
  {
    summary: RUN_QUARANTINED,
    events: RUN_QUARANTINED_EVENTS,
    transforms: RUN_QUARANTINED_TRANSFORMS,
    previewOf: RUN_QUARANTINED.id,
    sample: false,
    loaded: true,
  },
  {
    summary: RUN_CLEAN,
    events: RUN_CLEAN_EVENTS,
    transforms: RUN_CLEAN_TRANSFORMS,
    previewOf: RUN_CLEAN.id,
    sample: false,
    loaded: true,
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

/** A created run's copy of a fixture transform id (ids are globally unique). */
const transformIdFor = (runId: string, fixtureId: string) =>
  `${runId}_${fixtureId}`;

/**
 * Re-stamps run-repaired's log as a brand-new run starting at `createdAt`,
 * with this run's own transform ids.
 */
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
        // The file was never read: don't present the fixture's count as its.
        rows: null,
      };
    }
    if (e.type === "node_finished") {
      return {
        ...e,
        at,
        transformId: transformIdFor(summary.id, e.transformId),
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
      .sort((a, b) => b.createdAt.localeCompare(a.createdAt))
      .slice(0, MAX_LISTED_RUNS);
  }

  async createRun(file: File, table: TableName): Promise<RunSummary> {
    const summary: RunSummary = {
      id: `run-${this.#nextRun++}`,
      fileName: file.name,
      table,
      // The mock never reads the file, so it has no row count to report.
      rows: null,
      status: "queued",
      createdAt: new Date().toISOString(),
      costUsd: 0,
    };
    this.#runs.set(summary.id, {
      summary,
      events: replayAs(summary),
      transforms: RUN_REPAIRED_TRANSFORMS.map((t) => ({
        ...t,
        id: transformIdFor(summary.id, t.id),
      })),
      previewOf: RUN_REPAIRED.id,
      sample: true,
      loaded: false,
    });
    return { ...summary };
  }

  isSampleReplay(runId: string): boolean {
    return this.#runs.get(runId)?.sample ?? false;
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

  async getPreview(runId: string, page: PreviewPage): Promise<DataPreview> {
    const run = this.#get(runId);
    if (!run.loaded) {
      const detail = "The preview is available once the run has loaded.";
      throw new ApiError(409, `mock:/runs/${runId}/preview`, `409 ${detail}`, {
        detail,
      });
    }
    const fixture = this.#get(run.previewOf);
    return generatePreview(run.previewOf, fixture.summary.rows ?? 0, page);
  }

  async getEvalSummary(): Promise<EvalSummary> {
    return structuredClone(EVAL_SUMMARY);
  }

  #get(runId: string): MockRun {
    const run = this.#runs.get(runId);
    if (!run) {
      const detail = `Unknown run: ${runId}`;
      throw new ApiError(404, `mock:/runs/${runId}`, `404 ${detail}`, {
        detail,
      });
    }
    return run;
  }

  /**
   * Keeps a created run's summary in step with what has been streamed:
   * status, and `costUsd` as a running total until `run_finished`.
   */
  #track(run: MockRun, event: RunEvent): void {
    if (!run.sample) return;
    if (event.type === "run_started" && run.summary.status === "queued") {
      run.summary = { ...run.summary, status: "running" };
    } else if (
      event.type === "attempt_failed" ||
      event.type === "node_finished"
    ) {
      run.summary = {
        ...run.summary,
        costUsd: run.summary.costUsd + event.costUsd,
      };
    } else if (event.type === "loaded") {
      run.loaded = true;
    } else if (event.type === "run_finished") {
      run.summary = {
        ...run.summary,
        status: event.status,
        costUsd: event.costUsd,
      };
    }
  }
}
