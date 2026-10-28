import { describe, expect, test, vi } from "vitest";
import { ApiError, HttpApiClient } from "@/lib/api/http-client";
import type { RunEvent, RunSummary } from "@/lib/api/types";

const run: RunSummary = {
  id: "run-1",
  fileName: "orders.csv",
  table: "orders",
  rows: 10,
  status: "queued",
  createdAt: "2026-10-28T09:00:00.000Z",
  costUsd: 0,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sse(events: RunEvent[]): string {
  return events
    .map((e) => `id: ${e.seq}\ndata: ${JSON.stringify(e)}\n\n`)
    .join("");
}

const AT = "2026-10-28T09:00:00.000Z";

describe("HttpApiClient", () => {
  test("HttpApiClient.listRuns calls GET {base}/runs", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => jsonResponse([run]));
    vi.stubGlobal("fetch", fetchMock);

    const client = new HttpApiClient("http://api.test/");
    expect(client.isDemo).toBe(false);
    await expect(client.listRuns()).resolves.toEqual([run]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("http://api.test/runs");
    expect(init?.method ?? "GET").toBe("GET");
  });

  test("createRun posts multipart file and table", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => jsonResponse(run, 201));
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["a\n"], "orders.csv");
    await new HttpApiClient("http://api.test").createRun(file, "orders");

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("http://api.test/runs");
    expect(init?.method).toBe("POST");
    const body = init?.body;
    expect(body).toBeInstanceOf(FormData);
    const form = body instanceof FormData ? body : new FormData();
    expect(form.get("table")).toBe("orders");
    expect(form.get("file")).toBeInstanceOf(File);
  });

  test("getPreview passes offset and limit and encodes run id", async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      jsonResponse({ columns: [], rows: [], totalRows: 0, nextOffset: null }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await new HttpApiClient("http://api.test").getPreview("run/1", {
      offset: 0,
      limit: 50,
    });
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://api.test/runs/run%2F1/preview?offset=0&limit=50",
    );
  });

  test("non-2xx responses throw ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => jsonResponse({ detail: "nope" }, 404)),
    );
    await expect(
      new HttpApiClient("http://api.test").getEvalSummary(),
    ).rejects.toBeInstanceOf(ApiError);
  });

  test("streamEvents parses SSE and resumes with Last-Event-ID", async () => {
    const e1: RunEvent = {
      seq: 1,
      at: AT,
      type: "run_started",
      runId: "run-1",
      fileName: "orders.csv",
      table: "orders",
      rows: 10,
    };
    const e2: RunEvent = { seq: 2, at: AT, type: "rules_applied", actions: [] };
    const e3: RunEvent = {
      seq: 3,
      at: AT,
      type: "run_finished",
      status: "succeeded",
      summary: "ok",
      costUsd: 0,
      tokens: 0,
    };
    const bodies = [
      // First connection drops after seq 2 (with a comment line and CRLFs).
      `: keep-alive\r\n\r\n${sse([e1, e2])}`,
      // Server replays seq 2 (duplicate) then continues.
      sse([e2, e3]),
    ];
    const fetchMock = vi.fn<typeof fetch>(
      async () =>
        new Response(bodies.shift() ?? "", {
          headers: { "Content-Type": "text/event-stream" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const received: RunEvent[] = [];
    await new HttpApiClient("http://api.test", {
      reconnectDelayMs: 0,
    }).streamEvents(
      "run-1",
      (e) => received.push(e),
      new AbortController().signal,
    );

    expect(received.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "http://api.test/runs/run-1/events",
    );
    const headers = new Headers(fetchMock.mock.calls[1]?.[1]?.headers);
    expect(headers.get("Last-Event-ID")).toBe("2");
  });

  describe("readSse and reconnects", () => {
    const started: RunEvent = {
      seq: 1,
      at: AT,
      type: "run_started",
      runId: "run-1",
      fileName: "orders.csv",
      table: "orders",
      rows: 10,
    };
    const finished: RunEvent = {
      seq: 2,
      at: AT,
      type: "run_finished",
      status: "succeeded",
      summary: "ok",
      costUsd: 0,
      tokens: 0,
    };
    const signal = () => new AbortController().signal;

    test("cancels the body when it stops reading before the stream is done", async () => {
      const cancel = vi.fn<() => void>();
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(
            new TextEncoder().encode(sse([started, finished])),
          );
          // Never closes: the server would keep the connection open.
        },
        cancel,
      });
      vi.stubGlobal(
        "fetch",
        vi.fn<typeof fetch>(async () => new Response(body)),
      );
      const seen: number[] = [];
      await new HttpApiClient("http://api.test").streamEvents(
        "run-1",
        (e) => seen.push(e.seq),
        signal(),
      );
      expect(seen).toEqual([1, 2]);
      expect(cancel).toHaveBeenCalledTimes(1);
    });

    test("a 4xx is fatal: no reconnect", async () => {
      const fetchMock = vi.fn<typeof fetch>(async () =>
        jsonResponse({ detail: "Unknown run" }, 404),
      );
      vi.stubGlobal("fetch", fetchMock);
      const error = await new HttpApiClient("http://api.test", {
        reconnectDelayMs: 0,
      })
        .streamEvents("run-x", () => {}, signal())
        .catch((e: unknown) => e);
      expect(error).toBeInstanceOf(ApiError);
      expect(error).toMatchObject({ status: 404, detail: "Unknown run" });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    test("gives up after maxReconnects failed connections with the last error", async () => {
      const fetchMock = vi.fn<typeof fetch>(async () =>
        jsonResponse({ detail: "busy" }, 503),
      );
      vi.stubGlobal("fetch", fetchMock);
      const error = await new HttpApiClient("http://api.test", {
        reconnectDelayMs: 0,
        maxReconnects: 2,
      })
        .streamEvents("run-1", () => {}, signal())
        .catch((e: unknown) => e);
      expect(error).toMatchObject({ status: 503 });
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });

    test("an abort during the backoff resolves without reconnecting", async () => {
      vi.useFakeTimers();
      const fetchMock = vi.fn<typeof fetch>(async () => {
        throw new TypeError("Failed to fetch");
      });
      vi.stubGlobal("fetch", fetchMock);
      const controller = new AbortController();
      const done = new HttpApiClient("http://api.test", {
        reconnectDelayMs: 60_000,
      }).streamEvents("run-1", () => {}, controller.signal);
      await vi.advanceTimersByTimeAsync(10);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      controller.abort();
      await expect(done).resolves.toBeUndefined();
      await vi.advanceTimersByTimeAsync(120_000);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      vi.useRealTimers();
    });

    test("frames CR-only and CRLF lines, split across chunks", async () => {
      const text = `id: 1\rdata: ${JSON.stringify(started)}\r\r: ping\r\nid: 2\r\ndata: ${JSON.stringify(finished)}\r\n\r\n`;
      const bytes = new TextEncoder().encode(text);
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          // Split mid-"\r\n" and mid-JSON.
          for (let i = 0; i < bytes.length; i += 7) {
            controller.enqueue(bytes.slice(i, i + 7));
          }
          controller.close();
        },
      });
      vi.stubGlobal(
        "fetch",
        vi.fn<typeof fetch>(async () => new Response(body)),
      );
      const seen: string[] = [];
      await new HttpApiClient("http://api.test").streamEvents(
        "run-1",
        (e) => seen.push(e.type),
        signal(),
      );
      expect(seen).toEqual(["run_started", "run_finished"]);
    });

    test("skips a malformed data line and keeps the connection", async () => {
      const fetchMock = vi.fn<typeof fetch>(
        async () =>
          new Response(`id: 1\ndata: {not json\n\n${sse([started, finished])}`),
      );
      vi.stubGlobal("fetch", fetchMock);
      const seen: number[] = [];
      await new HttpApiClient("http://api.test", {
        reconnectDelayMs: 0,
      }).streamEvents("run-1", (e) => seen.push(e.seq), signal());
      expect(seen).toEqual([1, 2]);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
  });
});
