import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { ApiProvider } from "@/components/api/ApiProvider";
import { EvalsView } from "@/components/evals/EvalsView";
import { SidePanel } from "@/components/panel/SidePanel";
import { NewRunComposer } from "@/components/thread/NewRunComposer";
import { RunThread } from "@/components/thread/RunThread";
import { ApiError } from "@/lib/api/errors";
import { RUN_REPAIRED_EVENTS } from "@/lib/api/fixtures/run-repaired";
import { HttpApiClient } from "@/lib/api/http-client";
import { MockApiClient } from "@/lib/api/mock-client";
import type { ApiClient, RunEvent } from "@/lib/api/types";
import {
  INITIAL_RUN_VIEW_STATE,
  type RunViewState,
  reduceRunEvents,
} from "@/lib/run-state";

vi.mock("next/navigation", () => ({
  usePathname: () => "/runs",
  useRouter: () => ({ push: vi.fn<(href: string) => void>() }),
}));
vi.mock("@/lib/highlight", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/highlight")>()),
  highlight: () => new Promise(() => {}),
}));

const sse = (events: readonly RunEvent[]) =>
  events.map((e) => `id: ${e.seq}\ndata: ${JSON.stringify(e)}\n\n`).join("");

const stream = (body: string) =>
  new Response(body, { headers: { "Content-Type": "text/event-stream" } });

const json = (body: unknown, status: number) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const stateOf = (events: readonly RunEvent[]): RunViewState =>
  events.reduce<RunViewState>(reduceRunEvents, INITIAL_RUN_VIEW_STATE);

/** A non-demo client: the mock's data with selected methods overridden. */
function liveClient(overrides: Partial<ApiClient> = {}): ApiClient {
  const base = new MockApiClient({ delayMs: 0 });
  return {
    isDemo: false,
    listRuns: () => base.listRuns(),
    createRun: (f, t) => base.createRun(f, t),
    streamEvents: (id, on, s) => base.streamEvents(id, on, s),
    getTransforms: (id) => base.getTransforms(id),
    getPreview: (...args) => base.getPreview(...args),
    getInput: (...args) => base.getInput(...args),
    getEvalSummary: () => base.getEvalSummary(),
    ...overrides,
  };
}

const apiError = (status: number, detail: string) =>
  new ApiError(status, "http://api.test/x", `${status} ${detail}`, {
    detail,
  });

describe("stream outcome with the HTTP client", () => {
  test("a stream that streamed steps, dropped twice and gave up is 'Connection lost'", async () => {
    let calls = 0;
    const fetchMock = vi.fn<typeof fetch>(async () => {
      calls++;
      if (calls === 1) return stream(sse(RUN_REPAIRED_EVENTS.slice(0, 4)));
      throw new TypeError("Failed to fetch");
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new HttpApiClient("http://api.test", {
      reconnectDelayMs: 0,
      maxReconnects: 1,
    });
    render(
      <ApiProvider client={client}>
        <RunThread runId="run-repaired" />
      </ApiProvider>,
    );
    expect(await screen.findByText("Connection lost")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(screen.queryByText(/Couldn.t load this run/)).toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // Steps streamed before the drop are kept.
    expect(screen.getByText(/4 issues found/)).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  test("a stream that fails before any event is an error, not a lost connection", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const client = new HttpApiClient("http://api.test", {
      reconnectDelayMs: 0,
      maxReconnects: 1,
    });
    render(
      <ApiProvider client={client}>
        <RunThread runId="run-repaired" />
      </ApiProvider>,
    );
    expect(
      await screen.findByText("Couldn't load this run"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Connection lost")).toBeNull();
    vi.unstubAllGlobals();
  });

  test("a 404 says the run was not found", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => json({ detail: "Unknown run" }, 404)),
    );
    render(
      <ApiProvider client={new HttpApiClient("http://api.test")}>
        <RunThread runId="run-nope" />
      </ApiProvider>,
    );
    expect(await screen.findByText("Run not found")).toBeInTheDocument();
    expect(screen.getByText(/no run with this id/i)).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  test("the user bubble stops pulsing once the stream has errored", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => json({ detail: "Unknown run" }, 404)),
    );
    const { container } = render(
      <ApiProvider client={new HttpApiClient("http://api.test")}>
        <RunThread runId="run-nope" />
      </ApiProvider>,
    );
    await screen.findByText("Run not found");
    expect(container.querySelector(".animate-pulse")).toBeNull();
    expect(screen.getByText("Request details unavailable")).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});

describe("API error details", () => {
  test.each([
    [413, "File too large: the limit is 100 MB."],
    [415, "Unsupported file format: .xlsx"],
    [422, "Unknown table: invoices"],
  ])("composer shows the server's %i detail", async (status, detail) => {
    const client = liveClient({
      createRun: () => Promise.reject(apiError(status, detail)),
    });
    render(
      <ApiProvider client={client}>
        <NewRunComposer />
      </ApiProvider>,
    );
    const user = userEvent.setup();
    await user.upload(
      screen.getByLabelText("Attach a file"),
      new File(["a"], "a.csv", { type: "text/csv" }),
    );
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  });

  test.each([
    [403, "forbidden_origin", "This origin may not call the API."],
    [422, "too_large_decoded", "The Parquet file decodes to more than 1 GiB."],
  ])(
    "composer shows the HTTP client's %i %s detail",
    async (status, code, detail) => {
      vi.stubGlobal(
        "fetch",
        vi.fn<typeof fetch>(async () => json({ detail, code }, status)),
      );
      render(
        <ApiProvider client={new HttpApiClient("http://api.test")}>
          <NewRunComposer />
        </ApiProvider>,
      );
      const user = userEvent.setup();
      await user.upload(
        screen.getByLabelText("Attach a file"),
        new File(["a"], "a.parquet"),
      );
      await user.click(screen.getByRole("button", { name: "Start run" }));
      expect(await screen.findByRole("alert")).toHaveTextContent(detail);
      vi.unstubAllGlobals();
    },
  );

  test("composer refuses a file over 100 MB without uploading it", async () => {
    const createRun = vi.fn<ApiClient["createRun"]>();
    render(
      <ApiProvider client={liveClient({ createRun })}>
        <NewRunComposer />
      </ApiProvider>,
    );
    const user = userEvent.setup();
    const at = (size: number) => {
      const file = new File(["a"], "big.csv", { type: "text/csv" });
      Object.defineProperty(file, "size", { value: size });
      return file;
    };
    await user.upload(
      screen.getByLabelText("Attach a file"),
      at(100 * 1024 * 1024 + 1),
    );
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "File too large: the limit is 100 MB.",
    );
    expect(createRun).not.toHaveBeenCalled();

    // Exactly at the limit is allowed.
    createRun.mockReturnValue(new Promise(() => {}));
    await user.upload(
      screen.getByLabelText("Attach a file"),
      at(100 * 1024 * 1024),
    );
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(createRun).toHaveBeenCalledTimes(1);
  });

  test("composer keeps the connection copy for 5xx and network errors", async () => {
    const createRun = vi
      .fn<ApiClient["createRun"]>()
      .mockRejectedValueOnce(apiError(503, "upstream down"))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"));
    render(
      <ApiProvider client={liveClient({ createRun })}>
        <NewRunComposer />
      </ApiProvider>,
    );
    const user = userEvent.setup();
    await user.upload(
      screen.getByLabelText("Attach a file"),
      new File(["a"], "a.csv", { type: "text/csv" }),
    );
    for (let i = 0; i < 2; i++) {
      await user.click(screen.getByRole("button", { name: "Start run" }));
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(/Check the connection/);
      expect(alert).not.toHaveTextContent(/upstream/);
    }
  });

  test("the side panel shows a 4xx detail for the generated code", async () => {
    const user = userEvent.setup();
    const client = liveClient({
      getTransforms: () => Promise.reject(apiError(422, "No such run step")),
    });
    render(
      <ApiProvider client={client}>
        <SidePanel
          runId="run-repaired"
          state={stateOf(RUN_REPAIRED_EVENTS)}
          dataViewportHeight={480}
        />
      </ApiProvider>,
    );
    await user.click(screen.getByRole("tab", { name: "Code" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No such run step",
    );
  });

  test("evals shows a 4xx detail and the connection copy for 5xx", async () => {
    const getEvalSummary = vi
      .fn<ApiClient["getEvalSummary"]>()
      .mockRejectedValueOnce(apiError(404, "No eval results published"))
      .mockRejectedValueOnce(apiError(500, "boom"));
    render(
      <ApiProvider client={liveClient({ getEvalSummary })}>
        <EvalsView />
      </ApiProvider>,
    );
    const first = await screen.findByRole("alert");
    expect(first).toHaveTextContent("No eval results published");
    await userEvent.click(within(first).getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        /Check the connection/,
      ),
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent("boom");
  });
});

describe("demo honesty", () => {
  test("a run created through the mock says the file was not read and hides the fixture row count", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const run = await client.createRun(
      new File(["a,b\n"], "mine.csv", { type: "text/csv" }),
      "orders",
    );
    expect(run.rows).toBeNull();
    render(
      <ApiProvider client={client}>
        <RunThread runId={run.id} />
      </ApiProvider>,
    );
    expect(
      await screen.findByText(
        "Demo: your file wasn't uploaded or read — this replays a sample run.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("mine.csv · sample run")).toBeInTheDocument();
    expect(screen.queryByText(/mine\.csv · 48,210 rows/)).toBeNull();
  });

  test("the Data tab of a sample-replay run labels its rows as sample data", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const run = await client.createRun(
      new File(["a,b\n"], "mine.csv", { type: "text/csv" }),
      "orders",
    );
    // Replay the stream so the mock marks the run loaded (preview available).
    const events: RunEvent[] = [];
    await client.streamEvents(
      run.id,
      (e) => events.push(e),
      new AbortController().signal,
    );
    const state = events.reduce<RunViewState>(
      reduceRunEvents,
      INITIAL_RUN_VIEW_STATE,
    );
    render(
      <ApiProvider client={client}>
        <SidePanel runId={run.id} state={state} dataViewportHeight={480} />
      </ApiProvider>,
    );
    expect(
      await screen.findByText(
        /Sample run · showing 1,000 of 48,210 sample rows$/,
      ),
    ).toBeInTheDocument();
    // The fixture's load count is not the user's: not shown.
    expect(screen.queryByText(/loaded/)).toBeNull();
  });

  test("a fixture run's Data tab has no sample label", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    render(
      <ApiProvider client={client}>
        <SidePanel
          runId="run-repaired"
          state={RUN_REPAIRED_EVENTS.reduce<RunViewState>(
            reduceRunEvents,
            INITIAL_RUN_VIEW_STATE,
          )}
          dataViewportHeight={480}
        />
      </ApiProvider>,
    );
    expect(
      await screen.findByText(
        /Showing 1,000 of 48,210 uploaded rows · 48,210 loaded$/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Sample run/)).toBeNull();
  });

  test("a live run without a row count yet says it is counting", async () => {
    const client = liveClient({
      streamEvents: (_id, onEvent) => {
        onEvent({
          seq: 1,
          at: "2026-10-28T09:00:00.000Z",
          type: "run_started",
          runId: "run-x",
          fileName: "big.parquet",
          table: "lineitem",
          rows: null,
        });
        return new Promise(() => {});
      },
    });
    render(
      <ApiProvider client={client}>
        <RunThread runId="run-x" />
      </ApiProvider>,
    );
    expect(
      await screen.findByText("big.parquet · counting rows…"),
    ).toBeInTheDocument();
  });

  test("fixture runs show no replay note", async () => {
    render(
      <ApiProvider client={new MockApiClient({ delayMs: 0 })}>
        <RunThread runId="run-repaired" />
      </ApiProvider>,
    );
    await screen.findByText("orders_2026-10.csv · 48,210 rows");
    expect(screen.queryByText(/replays a sample run/)).toBeNull();
  });
});

describe("demo badge on number-bearing views", () => {
  test("evals cards carry a compact Demo badge with the mock", async () => {
    render(
      <ApiProvider client={new MockApiClient({ delayMs: 0 })}>
        <EvalsView />
      </ApiProvider>,
    );
    await screen.findByRole("region", {
      name: "Results by system, scrollable",
    });
    const results = screen.getByRole("region", { name: "Results" });
    expect(within(results).getByText("Demo data")).toBeInTheDocument();
    const pareto = screen.getByRole("region", { name: "Cost vs quality" });
    expect(within(pareto).getByText("Demo data")).toBeInTheDocument();
  });

  test("the run summary and side panel carry it with the mock", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    render(
      <ApiProvider client={client}>
        <RunThread runId="run-repaired" />
        <SidePanel
          runId="run-repaired"
          state={stateOf(RUN_REPAIRED_EVENTS)}
          dataViewportHeight={480}
        />
      </ApiProvider>,
    );
    const summary = await screen.findByRole("region", { name: "Run summary" });
    expect(within(summary).getByText("Demo data")).toBeInTheDocument();
    const tabs = screen.getByTestId("side-panel-tabs");
    expect(within(tabs).getByText("Demo data")).toBeInTheDocument();
  });

  test("no compact badges with a live client", async () => {
    render(
      <ApiProvider client={liveClient()}>
        <EvalsView />
        <RunThread runId="run-repaired" />
        <SidePanel
          runId="run-repaired"
          state={stateOf(RUN_REPAIRED_EVENTS)}
          dataViewportHeight={480}
        />
      </ApiProvider>,
    );
    await screen.findByRole("region", { name: "Run summary" });
    await screen.findByRole("region", {
      name: "Results by system, scrollable",
    });
    expect(screen.queryByText("Demo data")).toBeNull();
  });
});
