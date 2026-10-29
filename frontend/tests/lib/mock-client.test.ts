import { afterEach, describe, expect, test, vi } from "vitest";
import { getApiClient } from "@/lib/api/client";
import { HttpApiClient } from "@/lib/api/http-client";
import { MOCK_EVENT_DELAY_MS, MockApiClient } from "@/lib/api/mock-client";
import type { RunEvent } from "@/lib/api/types";

async function collect(
  client: MockApiClient,
  runId: string,
  signal = new AbortController().signal,
): Promise<RunEvent[]> {
  const events: RunEvent[] = [];
  await client.streamEvents(runId, (e) => events.push(e), signal);
  return events;
}

describe("MockApiClient", () => {
  test("mock streams run-repaired events in seq order ending with run_finished", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const events = await collect(client, "run-repaired");

    expect(events.length).toBeGreaterThan(2);
    events.forEach((e, i) => {
      expect(e.seq).toBe(i + 1);
    });
    expect(events[0]?.type).toBe("run_started");
    expect(events.at(-1)).toMatchObject({
      type: "run_finished",
      status: "succeeded",
    });

    const failed = events.find((e) => e.type === "attempt_failed");
    expect(failed).toMatchObject({
      attempt: 1,
      error: "contract check failed: 14 rows in paise",
    });
    expect(events.find((e) => e.type === "verified")).toMatchObject({
      checksPassed: 12,
      checksTotal: 12,
    });
    expect(events.find((e) => e.type === "loaded")).toMatchObject({
      rowsLoaded: 48_210,
    });
  });

  test("mock stream stops on abort", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const controller = new AbortController();
    const received: RunEvent[] = [];
    await client.streamEvents(
      "run-repaired",
      (e) => {
        received.push(e);
        if (received.length === 2) controller.abort();
      },
      controller.signal,
    );
    expect(received).toHaveLength(2);
  });

  test("uses a 400 ms default delay and isDemo", () => {
    expect(MOCK_EVENT_DELAY_MS).toBe(400);
    expect(new MockApiClient().isDemo).toBe(true);
  });

  test("lists the three fixture runs", async () => {
    const runs = await new MockApiClient().listRuns();
    const byId = Object.fromEntries(runs.map((r) => [r.id, r]));
    expect(byId["run-clean"]).toMatchObject({
      table: "orders",
      status: "succeeded",
      costUsd: 0,
    });
    expect(byId["run-repaired"]).toMatchObject({
      fileName: "orders_2026-10.csv",
      rows: 48_210,
      status: "succeeded",
    });
    expect(byId["run-quarantined"]).toMatchObject({
      table: "lineitem",
      status: "quarantined",
    });
  });

  test("run-clean plans no nodes; run-quarantined quarantines 212 rows", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const clean = await collect(client, "run-clean");
    expect(clean.find((e) => e.type === "plan_created")).toMatchObject({
      nodes: [],
    });
    const quarantined = await collect(client, "run-quarantined");
    expect(quarantined.find((e) => e.type === "loaded")).toMatchObject({
      rowsQuarantined: 212,
    });
    expect(quarantined.at(-1)).toMatchObject({
      type: "run_finished",
      status: "quarantined",
    });
  });

  test("createRun returns run-<n> that replays run-repaired with the new id", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const file = new File(["a,b\n"], "my_orders.csv", { type: "text/csv" });
    const first = await client.createRun(file, "orders");
    const second = await client.createRun(file, "orders");
    expect(first.id).toBe("run-1");
    expect(second.id).toBe("run-2");
    expect(first).toMatchObject({
      fileName: "my_orders.csv",
      status: "queued",
    });

    const events = await collect(client, first.id);
    const repaired = await collect(client, "run-repaired");
    // A live replay adds `activity` lines between the fixture's events.
    expect(
      events.filter((e) => e.type !== "activity").map((e) => e.type),
    ).toEqual(repaired.map((e) => e.type));
    expect(events[0]).toMatchObject({
      type: "run_started",
      runId: "run-1",
      fileName: "my_orders.csv",
    });
    expect((await client.listRuns()).map((r) => r.id)).toContain("run-1");
  });

  test("transforms: failing node has attempts 1 and 2, other node has one", async () => {
    const transforms = await new MockApiClient().getTransforms("run-repaired");
    const byNode = Map.groupBy(transforms, (t) => t.nodeId);
    const counts = [...byNode.values()].map((ts) => ts.length).sort();
    expect(counts).toEqual([1, 2]);
    const retried = [...byNode.values()].find((ts) => ts.length === 2) ?? [];
    expect(retried.map((t) => [t.attempt, t.passed])).toEqual([
      [1, false],
      [2, true],
    ]);
  });

  test("getPreview is deterministic and capped", async () => {
    const client = new MockApiClient();
    const a = await client.getPreview("run-repaired", { offset: 0, limit: 25 });
    const b = await client.getPreview("run-repaired", { offset: 0, limit: 25 });
    expect(a.rows).toHaveLength(25);
    expect(a).toEqual(b);
    expect(a.totalRows).toBe(48_210);

    const big = await client.getPreview("run-repaired", {
      offset: 0,
      limit: 1_000_000,
    });
    expect(big.rows).toHaveLength(1_000);
    expect(big.rows.slice(0, 25)).toEqual(a.rows);
    for (const row of big.rows.slice(0, 200)) {
      expect(Object.keys(row.cells).sort()).toEqual([...big.columns].sort());
    }
  });

  test("eval summary has 3 systems and a 5-point router line", async () => {
    const summary = await new MockApiClient().getEvalSummary();
    expect(summary.systems.map((s) => s.system)).toEqual([
      "rules",
      "always-strong",
      "sluice",
    ]);
    const baseline = summary.pareto.filter((p) => p.kind === "baseline");
    expect(baseline.flatMap((p) => p.points)).toHaveLength(3);
    const router = summary.pareto.filter((p) => p.kind === "router");
    expect(router).toHaveLength(1);
    expect(router[0]?.points).toHaveLength(5);
    expect(router[0]?.points.every((p) => typeof p.lambda === "number")).toBe(
      true,
    );
  });

  test("unknown run ids reject with a 404", async () => {
    await expect(new MockApiClient().getTransforms("run-nope")).rejects.toThrow(
      "Unknown run: run-nope",
    );
    await expect(
      new MockApiClient().getTransforms("run-nope"),
    ).rejects.toMatchObject({ status: 404 });
  });
});

describe("getApiClient", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  test("getApiClient returns mock when env unset", () => {
    vi.stubEnv("NEXT_PUBLIC_SLUICE_API_URL", undefined);
    const client = getApiClient();
    expect(client.isDemo).toBe(true);
    expect(client).toBeInstanceOf(MockApiClient);
  });

  test('returns mock when env is "mock"', () => {
    vi.stubEnv("NEXT_PUBLIC_SLUICE_API_URL", "mock");
    expect(getApiClient().isDemo).toBe(true);
  });

  test("returns the HTTP client when env is a URL", () => {
    vi.stubEnv("NEXT_PUBLIC_SLUICE_API_URL", "http://localhost:8080");
    const client = getApiClient();
    expect(client).toBeInstanceOf(HttpApiClient);
    expect(client.isDemo).toBe(false);
  });
});
