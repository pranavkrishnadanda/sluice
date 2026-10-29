import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { ApiProvider } from "@/components/api/ApiProvider";
import { SidePanel } from "@/components/panel/SidePanel";
import { ApiError } from "@/lib/api/errors";
import { RUN_REPAIRED_EVENTS } from "@/lib/api/fixtures/run-repaired";
import { MockApiClient } from "@/lib/api/mock-client";
import type { ApiClient, RawInput, RunEvent } from "@/lib/api/types";
import {
  INITIAL_RUN_VIEW_STATE,
  type RunViewState,
  reduceRunEvents,
} from "@/lib/run-state";

vi.mock("@/lib/highlight", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/highlight")>()),
  highlight: () => new Promise(() => {}),
}));

const stateOf = (events: readonly RunEvent[]): RunViewState =>
  events.reduce<RunViewState>(reduceRunEvents, INITIAL_RUN_VIEW_STATE);

const loadedAt = RUN_REPAIRED_EVENTS.findIndex((e) => e.type === "loaded");
const BEFORE_LOAD = stateOf(RUN_REPAIRED_EVENTS.slice(0, loadedAt));
const REFUSED = stateOf([
  ...RUN_REPAIRED_EVENTS.slice(0, 1),
  {
    seq: 2,
    at: "2026-10-27T10:14:04.000Z",
    type: "run_finished",
    status: "quarantined",
    summary: "This looks like a lineitem file; nothing loaded.",
    costUsd: 0,
    tokens: 0,
  },
]);

function client(over: Partial<ApiClient> = {}): ApiClient {
  const base = new MockApiClient({ delayMs: 0 });
  return {
    isDemo: false,
    listRuns: () => base.listRuns(),
    createRun: (f, t) => base.createRun(f, t),
    streamEvents: (id, on, s) => base.streamEvents(id, on, s),
    getTransforms: (id) => base.getTransforms(id),
    getPreview: (id, p) => base.getPreview(id, p),
    getInput: (id, p, s) => base.getInput(id, p, s),
    getEvalSummary: () => base.getEvalSummary(),
    ...over,
  };
}

function renderPanel(state: RunViewState | null, api: ApiClient = client()) {
  return render(
    <ApiProvider client={api}>
      <SidePanel runId="run-repaired" state={state} dataViewportHeight={480} />
    </ApiProvider>,
  );
}

describe("raw uploaded file in the Data tab", () => {
  test("before the run loads, the uploaded rows show, labelled as not loaded", async () => {
    const getPreview = vi.fn<ApiClient["getPreview"]>();
    renderPanel(BEFORE_LOAD, client({ getPreview }));
    const table = await screen.findByRole("table", { name: "Uploaded file" });
    expect(
      screen.getByRole("heading", { name: "Uploaded file (not loaded)" }),
    ).toBeInTheDocument();
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(headers).toEqual([
      "#",
      "o_orderkey",
      "o_custkey",
      "o_orderdate",
      "amt",
    ]);
    await waitFor(() =>
      expect(within(table).getAllByRole("row").length).toBeGreaterThan(3),
    );
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent(/^1/);
    expect(
      screen.getByText(/^Showing 1,000 of 48,210 uploaded rows · /),
    ).toBeInTheDocument();
    expect(getPreview).not.toHaveBeenCalled();
  });

  test("a run that never loaded still shows the file it was given", async () => {
    renderPanel(REFUSED);
    expect(
      await screen.findByRole("table", { name: "Uploaded file" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/didn.t load any data/i)).toBeInTheDocument();
  });

  test("once loaded, the before/after preview replaces it", async () => {
    const view = renderPanel(BEFORE_LOAD);
    await screen.findByRole("table", { name: "Uploaded file" });
    view.rerender(
      <ApiProvider client={client()}>
        <SidePanel
          runId="run-repaired"
          state={stateOf(RUN_REPAIRED_EVENTS)}
          dataViewportHeight={480}
        />
      </ApiProvider>,
    );
    expect(
      await screen.findByRole("table", { name: "Before and after values" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("table", { name: "Uploaded file" }),
    ).not.toBeInTheDocument();
  });

  test("nulls read as null and pages load as the window needs rows", async () => {
    const page = (offset: number): RawInput => ({
      columns: ["a"],
      rows: Array.from({ length: 5 }, (_, i) => [
        i === 0 ? null : `${offset + i}`,
      ]),
      totalRows: 10,
      nextOffset: offset === 0 ? 5 : null,
    });
    const getInput = vi.fn<ApiClient["getInput"]>(async (_id, p) =>
      page(p.offset),
    );
    renderPanel(BEFORE_LOAD, client({ getInput }));
    const table = await screen.findByRole("table", { name: "Uploaded file" });
    await waitFor(() => expect(getInput).toHaveBeenCalledTimes(2));
    expect(getInput.mock.calls[1]?.[1]).toEqual({ offset: 5, limit: 1_000 });
    await waitFor(() =>
      expect(within(table).getAllByRole("row")).toHaveLength(11),
    );
    expect(within(table).getAllByText("null")).toHaveLength(2);
    expect(table).toHaveAttribute("aria-rowcount", "11");
  });

  test("an unavailable upload (404) says so instead of an error", async () => {
    const getInput = vi.fn<ApiClient["getInput"]>(async () => {
      throw new ApiError(404, "x", "404 Unknown run", {
        detail: "Unknown run",
      });
    });
    renderPanel(REFUSED, client({ getInput }));
    expect(
      await screen.findByText(/uploaded file isn.t available/i),
    ).toBeInTheDocument();
  });

  test("a demo replay labels the rows as sample rows", async () => {
    const base = new MockApiClient({ delayMs: 0 });
    const run = await base.createRun(new File(["a"], "a.csv"), "auto");
    render(
      <ApiProvider client={base}>
        <SidePanel
          runId={run.id}
          state={BEFORE_LOAD}
          dataViewportHeight={480}
        />
      </ApiProvider>,
    );
    await screen.findByRole("table", { name: "Uploaded file" });
    expect(
      screen.getByText(/Sample run · showing 1,000 of 48,210 sample rows/),
    ).toBeInTheDocument();
  });

  describe("fetching the upload only while it is wanted", () => {
    const LOADED = stateOf(RUN_REPAIRED_EVENTS);
    const onePage = async (): Promise<RawInput> => ({
      columns: ["a"],
      rows: [["1"]],
      totalRows: 1,
      nextOffset: null,
    });
    const panel = (state: RunViewState, runId = "run-repaired") => (
      <SidePanel runId={runId} state={state} dataViewportHeight={480} />
    );

    test("reopening a finished, loaded run never asks for the upload", async () => {
      const getInput = vi.fn<ApiClient["getInput"]>(onePage);
      const api = client({ getInput });
      // A run page starts from the initial state; the replay delivers the
      // rest (here in one later render, as a burst would).
      const view = render(
        <ApiProvider client={api}>{panel(INITIAL_RUN_VIEW_STATE)}</ApiProvider>,
      );
      expect(
        screen.queryByRole("status", { name: "Loading the uploaded file" }),
      ).not.toBeInTheDocument();
      view.rerender(
        <ApiProvider client={api}>
          {panel(stateOf(RUN_REPAIRED_EVENTS.slice(0, 1)))}
        </ApiProvider>,
      );
      view.rerender(<ApiProvider client={api}>{panel(LOADED)}</ApiProvider>);
      expect(
        await screen.findByRole("table", { name: "Before and after values" }),
      ).toBeInTheDocument();
      await new Promise((r) => setTimeout(r, 400));
      expect(getInput).not.toHaveBeenCalled();
    });

    test("a running run that has not loaded asks for it once", async () => {
      const getInput = vi.fn<ApiClient["getInput"]>(onePage);
      renderPanel(BEFORE_LOAD, client({ getInput }));
      await screen.findByRole("table", { name: "Uploaded file" });
      await new Promise((r) => setTimeout(r, 100));
      expect(getInput).toHaveBeenCalledTimes(1);
      expect(getInput.mock.calls[0]?.[2]).toBeInstanceOf(AbortSignal);
    });

    test("a pending request aborts when the run loads, changes or the panel closes", async () => {
      const signals: AbortSignal[] = [];
      const getInput = vi.fn<ApiClient["getInput"]>((_id, _p, signal) => {
        if (signal) signals.push(signal);
        return new Promise(() => {});
      });
      const api = client({ getInput });
      const view = render(
        <ApiProvider client={api}>{panel(BEFORE_LOAD)}</ApiProvider>,
      );
      await waitFor(() => expect(signals).toHaveLength(1));
      view.rerender(<ApiProvider client={api}>{panel(LOADED)}</ApiProvider>);
      await waitFor(() => expect(signals[0]?.aborted).toBe(true));

      view.rerender(
        <ApiProvider client={api}>
          {panel(BEFORE_LOAD, "run-other")}
        </ApiProvider>,
      );
      await waitFor(() => expect(signals).toHaveLength(2));
      view.rerender(
        <ApiProvider client={api}>
          {panel(BEFORE_LOAD, "run-third")}
        </ApiProvider>,
      );
      await waitFor(() => expect(signals[1]?.aborted).toBe(true));
      await waitFor(() => expect(signals).toHaveLength(3));
      view.unmount();
      expect(signals[2]?.aborted).toBe(true);
    });
  });
});
