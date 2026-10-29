import { ApiError } from "./errors";
import { EVAL_SUMMARY } from "./fixtures/eval-summary";
import { generateInput, generatePreview } from "./fixtures/preview";
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
import { RUN_REPAIRED_LIVE_EVENTS } from "./fixtures/run-repaired-live";
import type {
  ApiClient,
  DataPreview,
  EvalSummary,
  PreviewPage,
  RawInput,
  RunEvent,
  RunSummary,
  TableChoice,
  Transform,
} from "./types";

/** Default pause between replayed events, so the live timeline is watchable. */
export const MOCK_EVENT_DELAY_MS = 400;
/**
 * An `activity` phase is held this many event delays before the next event,
 * so its spinner and ticking clock are seen; a `waiting` back-off is held
 * for its `retryInS` (both scaled with `delayMs`, so 0 stays instant).
 */
const ACTIVITY_HOLD = 4;

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
 * run-repaired's live log (with `activity` lines) as a brand-new run, with
 * this run's own transform ids. `at` is stamped as each event streams.
 */
function replayAs(summary: RunSummary): RunEvent[] {
  return RUN_REPAIRED_LIVE_EVENTS.map((e) => {
    if (e.type === "run_started") {
      return {
        ...e,
        runId: summary.id,
        fileName: summary.fileName,
        table: summary.table,
        // The file was never read: don't present the fixture's count as its.
        rows: null,
      };
    }
    if (e.type === "node_finished") {
      return { ...e, transformId: transformIdFor(summary.id, e.transformId) };
    }
    return { ...e };
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

  async createRun(file: File, table: TableChoice): Promise<RunSummary> {
    const summary: RunSummary = {
      id: `run-${this.#nextRun++}`,
      fileName: file.name,
      // The mock can't read the file: "auto" resolves to the table of the
      // sample run it replays.
      table: table === "auto" ? RUN_REPAIRED.table : table,
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
    let previous: RunEvent | null = null;
    for (const event of run.events) {
      if (previous && this.#delayMs > 0) {
        await pause(this.#holdAfter(previous), signal);
      }
      if (signal.aborted) return;
      // A created run is happening now: its clock is the wall clock.
      const sent = run.sample
        ? { ...event, at: new Date().toISOString() }
        : event;
      this.#track(run, sent);
      onEvent(structuredClone(sent));
      previous = event;
    }
  }

  /** How long the replay waits after `event` before streaming the next. */
  #holdAfter(event: RunEvent): number {
    if (event.type !== "activity") return this.#delayMs;
    const scale = this.#delayMs / MOCK_EVENT_DELAY_MS;
    const backOff = (event.retryInS ?? 0) * 1_000 * scale;
    return Math.max(this.#delayMs * ACTIVITY_HOLD, backOff);
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

  async getInput(runId: string, page: PreviewPage): Promise<RawInput> {
    // Available from creation, like the server. A created run's file was
    // never read, so its rows are the sample run's (the UI says so).
    const run = this.#get(runId);
    const fixture = this.#get(run.previewOf);
    return generateInput(run.previewOf, fixture.summary.rows ?? 0, page);
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
