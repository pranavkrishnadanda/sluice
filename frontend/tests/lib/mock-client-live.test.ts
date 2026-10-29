import { describe, expect, test, vi } from "vitest";
import { MockApiClient } from "@/lib/api/mock-client";
import type { RunEvent } from "@/lib/api/types";

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

describe("mock live replay", () => {
  test("a created run streams activity lines with a gapless seq", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const run = await client.createRun(csv(), "auto");
    const events = await collect(client, run.id);
    expect(events.map((e) => e.seq)).toEqual(events.map((_, i) => i + 1));
    const activity = events.filter((e) => e.type === "activity");
    expect(activity.length).toBeGreaterThanOrEqual(5);
    expect(new Set(activity.map((e) => e.phase))).toEqual(
      new Set([
        "planning",
        "writing",
        "sandbox",
        "waiting",
        "verifying",
        "loading",
        "previewing",
      ]),
    );
    const waiting = activity.find((e) => e.phase === "waiting");
    expect(waiting?.retryInS).toBeGreaterThan(0);
    expect(waiting?.nodeId).toBe("n1");
    // Backend semantics: the current attempt, then the interrupted phase again.
    expect(waiting?.attempt).toBe(2);
    const after =
      activity[activity.indexOf(waiting as (typeof activity)[number]) + 1];
    expect(after).toMatchObject({ phase: "writing", attempt: 2 });
  });

  test("a created run's events are stamped as they stream", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(Date.parse("2026-10-29T12:00:00.000Z"));
      const client = new MockApiClient({ delayMs: 0 });
      const run = await client.createRun(csv(), "orders");
      vi.setSystemTime(Date.parse("2026-10-29T12:05:00.000Z"));
      const events = await collect(client, run.id);
      for (const e of events) expect(e.at).toBe("2026-10-29T12:05:00.000Z");
    } finally {
      vi.useRealTimers();
    }
  });

  test("node_started carries the attempt's tier: the retry runs on strong", async () => {
    const events = await collect(
      new MockApiClient({ delayMs: 0 }),
      "run-repaired",
    );
    const starts = events.flatMap((e) =>
      e.type === "node_started" ? [[e.nodeId, e.attempt, e.tier]] : [],
    );
    expect(starts).toEqual([
      ["n1", 1, "small"],
      ["n1", 2, "strong"],
      ["n2", 1, "small"],
    ]);
  });

  test("auto resolves to the replayed fixture's table", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const run = await client.createRun(csv(), "auto");
    expect(run.table).toBe("orders");
    const [first] = await collect(client, run.id);
    expect(first).toMatchObject({ type: "run_started", table: "orders" });
  });
});

describe("mock raw input", () => {
  test("is available for a created run before it loads", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const run = await client.createRun(csv(), "orders");
    await expect(
      client.getPreview(run.id, { offset: 0, limit: 10 }),
    ).rejects.toMatchObject({ status: 409 });
    const input = await client.getInput(run.id, { offset: 0, limit: 10 });
    expect(input.columns).toEqual([
      "o_orderkey",
      "o_custkey",
      "o_orderdate",
      "amt",
    ]);
    expect(input.rows).toHaveLength(10);
    for (const row of input.rows) expect(row).toHaveLength(4);
    expect(input.nextOffset).toBe(10);
  });

  test("raw values are the file's own, before any repair", async () => {
    const client = new MockApiClient();
    const page = { offset: 0, limit: 200 };
    const input = await client.getInput("run-repaired", page);
    const preview = await client.getPreview("run-repaired", page);
    input.rows.forEach((row, i) => {
      const p = preview.rows[i];
      expect(row[0]).toBe(p?.pk);
      expect(row[3]).toBe(p?.cells.o_totalprice?.before);
    });
  });

  test("lineitem's composite key splits into its own columns", async () => {
    const input = await new MockApiClient().getInput("run-quarantined", {
      offset: 0,
      limit: 5,
    });
    expect(input.columns).toEqual([
      "l_linenumber",
      "l_orderkey",
      "l_discount",
      "l_shipmode",
    ]);
    expect(input.rows[1]?.[0]).toBe("2");
  });

  test("is capped at 1,000 rows a page and ends with a null nextOffset", async () => {
    const client = new MockApiClient();
    const big = await client.getInput("run-clean", { offset: 0, limit: 1e6 });
    expect(big.rows).toHaveLength(1_000);
    const last = await client.getInput("run-clean", {
      offset: big.totalRows - 3,
      limit: 1_000,
    });
    expect(last.rows).toHaveLength(3);
    expect(last.nextOffset).toBeNull();
  });

  test("an unknown run is a 404", async () => {
    await expect(
      new MockApiClient().getInput("run-nope", { offset: 0, limit: 5 }),
    ).rejects.toMatchObject({ status: 404 });
  });

  test("an aborted request rejects instead of answering", async () => {
    await expect(
      new MockApiClient().getInput(
        "run-repaired",
        { offset: 0, limit: 5 },
        AbortSignal.abort(),
      ),
    ).rejects.toMatchObject({ name: "AbortError" });
  });
});
