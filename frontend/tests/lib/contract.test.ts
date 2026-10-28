import { describe, expect, test, vi } from "vitest";
import { buildSteps } from "@/components/thread/steps";
import { ApiError } from "@/lib/api/errors";
import { EVAL_SUMMARY } from "@/lib/api/fixtures/eval-summary";
import { RUN_CLEAN_EVENTS } from "@/lib/api/fixtures/run-clean";
import { RUN_QUARANTINED_EVENTS } from "@/lib/api/fixtures/run-quarantined";
import { RUN_REPAIRED_EVENTS } from "@/lib/api/fixtures/run-repaired";
import { HttpApiClient } from "@/lib/api/http-client";
import { MockApiClient } from "@/lib/api/mock-client";
import { PREVIEW_MAX_LIMIT, type RunEvent } from "@/lib/api/types";
import {
  INITIAL_RUN_VIEW_STATE,
  type RunViewState,
  reduceRunEvents,
} from "@/lib/run-state";

const stateOf = (events: readonly RunEvent[]): RunViewState =>
  events.reduce<RunViewState>(reduceRunEvents, INITIAL_RUN_VIEW_STATE);

async function collect(client: MockApiClient, runId: string) {
  const events: RunEvent[] = [];
  await client.streamEvents(
    runId,
    (e) => events.push(e),
    new AbortController().signal,
  );
  return events;
}

const csv = () => new File(["a,b\n"], "mine.csv", { type: "text/csv" });

describe("ids (B4)", () => {
  test("ids are opaque [A-Za-z0-9_-]{1,64}", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    await client.createRun(csv(), "orders");
    const ID = /^[A-Za-z0-9_-]{1,64}$/;
    for (const run of await client.listRuns()) {
      expect(run.id).toMatch(ID);
      for (const t of await client.getTransforms(run.id)) {
        expect(t.id).toMatch(ID);
      }
    }
  });

  test("a created run's transform ids are unique and match node_finished", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const run = await client.createRun(csv(), "orders");
    const fixtureIds = new Set(
      (await client.getTransforms("run-repaired")).map((t) => t.id),
    );
    const transforms = await client.getTransforms(run.id);
    for (const t of transforms) expect(fixtureIds.has(t.id)).toBe(false);
    const ids = new Set(transforms.map((t) => t.id));
    const events = await collect(client, run.id);
    const finished = events.filter((e) => e.type === "node_finished");
    expect(finished.length).toBeGreaterThan(0);
    for (const e of finished) expect(ids.has(e.transformId)).toBe(true);
  });
});

describe("stream semantics (B5)", () => {
  test.each([
    ["run-repaired", RUN_REPAIRED_EVENTS],
    ["run-quarantined", RUN_QUARANTINED_EVENTS],
    ["run-clean", RUN_CLEAN_EVENTS],
  ])(
    "%s: every node_started carries maxAttempts and one run_finished ends it",
    (_id, events) => {
      const budgets = events.flatMap((e) =>
        e.type === "node_started" ? [e.maxAttempts] : [],
      );
      expect(budgets.every((n) => n === 3)).toBe(true);
      expect(events.filter((e) => e.type === "run_finished")).toHaveLength(1);
      expect(events.at(-1)?.type).toBe("run_finished");
    },
  );

  test("a node shows 'attempt 1/3' from its first node_started", () => {
    const start = RUN_REPAIRED_EVENTS.findIndex(
      (e) => e.type === "node_started",
    );
    const state = stateOf(RUN_REPAIRED_EVENTS.slice(0, start + 1));
    expect(state.nodes[0]?.maxAttempts).toBe(3);
    const step = buildSteps(state, true).find((s) => s.id === "node-n1");
    expect(step?.meta).toBe("Writing a transform… · attempt 1/3");
  });

  test("an over-long attempt error is capped at 2,000 characters", () => {
    const state = stateOf([
      ...RUN_REPAIRED_EVENTS.slice(0, 5),
      {
        seq: 6,
        at: "2026-10-27T10:14:09.000Z",
        type: "attempt_failed",
        nodeId: "n1",
        attempt: 1,
        maxAttempts: 3,
        error: "x".repeat(5_000),
        tokens: 0,
        costUsd: 0,
      },
    ]);
    expect(state.nodes[0]?.lastError).toHaveLength(2_000);
  });

  test("unknown event types only advance seq", () => {
    const before = stateOf(RUN_REPAIRED_EVENTS.slice(0, 2));
    const unknown = {
      seq: 3,
      at: "2026-10-27T10:14:05.000Z",
      type: "future_thing",
      extra: 1,
    } as unknown as RunEvent;
    const after = reduceRunEvents(before, unknown);
    expect(after.lastSeq).toBe(3);
    expect({ ...after, lastSeq: 2 }).toEqual(before);
  });
});

describe("preview paging (B6)", () => {
  test("honours offset/limit, caps limit at the server max and reports nextOffset", async () => {
    const client = new MockApiClient();
    const first = await client.getPreview("run-repaired", {
      offset: 0,
      limit: 50_000,
    });
    expect(PREVIEW_MAX_LIMIT).toBe(1_000);
    expect(first.rows).toHaveLength(1_000);
    expect(first.nextOffset).toBe(1_000);
    expect(first.totalRows).toBe(48_210);

    const window = await client.getPreview("run-repaired", {
      offset: 10,
      limit: 5,
    });
    expect(window.rows).toEqual(first.rows.slice(10, 15));
    expect(window.nextOffset).toBe(15);

    const last = await client.getPreview("run-repaired", {
      offset: 48_000,
      limit: 1_000,
    });
    expect(last.rows).toHaveLength(210);
    expect(last.nextOffset).toBeNull();
  });

  test("rows carry quarantined/reason; lineitem pks are 'l_orderkey|l_linenumber'", async () => {
    const client = new MockApiClient();
    const page = await client.getPreview("run-quarantined", {
      offset: 0,
      limit: 1_000,
    });
    expect(page.rows[0]?.pk).toMatch(/^\d+\|\d+$/);
    const held = page.rows.filter((r) => r.quarantined);
    expect(held.length).toBeGreaterThan(0);
    for (const r of held) expect(r.reason).toMatch(/l_orderkey/);
    for (const r of page.rows.filter((x) => !x.quarantined)) {
      expect(r.reason).toBeNull();
    }
  });

  test("a created run's preview is 409 until it has loaded", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const run = await client.createRun(csv(), "orders");
    const error = await client
      .getPreview(run.id, { offset: 0, limit: 10 })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409 });
    await collect(client, run.id);
    const page = await client.getPreview(run.id, { offset: 0, limit: 10 });
    expect(page.rows).toHaveLength(10);
  });

  test("the HTTP client sends offset and limit", async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () =>
        new Response(
          JSON.stringify({
            columns: [],
            rows: [],
            totalRows: 0,
            nextOffset: null,
          }),
        ),
    );
    vi.stubGlobal("fetch", fetchMock);
    await new HttpApiClient("http://api.test").getPreview("run/1", {
      offset: 2_000,
      limit: 1_000,
    });
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://api.test/runs/run%2F1/preview?offset=2000&limit=1000",
    );
    vi.unstubAllGlobals();
  });
});

describe("eval summary (B8)", () => {
  test("pass^4, contract pass rate and p95 latency are intervals", () => {
    const sluice = EVAL_SUMMARY.systems.find((s) => s.system === "sluice");
    expect(sluice?.passHat4).toMatchObject({
      mean: expect.any(Number),
      lo: expect.any(Number),
      hi: expect.any(Number),
    });
    expect(sluice?.contractPassRate?.mean).toBeGreaterThan(0);
    expect(sluice?.p95LatencyMs?.mean).toBeGreaterThan(0);
  });
});

describe("run summaries (B9)", () => {
  test("costUsd is a running total while a created run streams", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const run = await client.createRun(csv(), "orders");
    const seen: number[] = [];
    await client.streamEvents(
      run.id,
      (e) => {
        if (e.type !== "attempt_failed" && e.type !== "node_finished") return;
        // Read synchronously: the summary has already been updated.
        void client.listRuns().then((runs) => {
          seen.push(runs.find((r) => r.id === run.id)?.costUsd ?? -1);
        });
      },
      new AbortController().signal,
    );
    await Promise.resolve();
    expect(seen[0]).toBeCloseTo(0.0011, 10);
    expect(seen.at(-1)).toBeCloseTo(0.0204, 10);
  });

  test("listRuns is newest first and at most 100", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    for (let i = 0; i < 105; i++) await client.createRun(csv(), "orders");
    const runs = await client.listRuns();
    expect(runs).toHaveLength(100);
    const times = runs.map((r) => r.createdAt);
    expect([...times].sort().reverse()).toEqual(times);
  });
});
