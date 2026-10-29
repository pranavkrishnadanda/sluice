import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { ApiProvider } from "@/components/api/ApiProvider";
import { DATA_ROW_HEIGHT } from "@/components/panel/DataTab";
import { SidePanel } from "@/components/panel/SidePanel";
import { ApiError } from "@/lib/api/errors";
import { RUN_CLEAN_EVENTS } from "@/lib/api/fixtures/run-clean";
import { RUN_REPAIRED_EVENTS } from "@/lib/api/fixtures/run-repaired";
import { MockApiClient } from "@/lib/api/mock-client";
import type { ApiClient, DataPreview, RunEvent } from "@/lib/api/types";
import {
  INITIAL_RUN_VIEW_STATE,
  type RunViewState,
  reduceRunEvents,
} from "@/lib/run-state";

// Shiki is exercised in tests/lib/highlight.test.ts; here the code view
// renders its plain-text fallback so tests don't wait on grammar loading.
vi.mock("@/lib/highlight", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/highlight")>()),
  highlight: () => new Promise(() => {}),
}));

const stateOf = (events: readonly RunEvent[]): RunViewState =>
  events.reduce<RunViewState>(reduceRunEvents, INITIAL_RUN_VIEW_STATE);

function renderPanel(
  runId: string,
  state: RunViewState | null,
  client: ApiClient = new MockApiClient({ delayMs: 0 }),
) {
  return render(
    <ApiProvider client={client}>
      <SidePanel runId={runId} state={state} dataViewportHeight={480} />
    </ApiProvider>,
  );
}

/** A fully loaded (single-page) preview of `n` rows, half of them changed. */
function bigPreview(n: number): DataPreview {
  return {
    columns: ["a", "b"],
    totalRows: n,
    nextOffset: null,
    rows: Array.from({ length: n }, (_, i) => ({
      pk: String(i + 1),
      quarantined: false,
      reason: null,
      cells: {
        a: { before: `${i},0`, after: `${i}.0`, flagged: false },
        b: { before: "x", after: i % 2 ? "x" : "y", flagged: false },
      },
    })),
  };
}

function withPreview(preview: DataPreview): ApiClient {
  const base = new MockApiClient({ delayMs: 0 });
  return {
    isDemo: true,
    listRuns: () => base.listRuns(),
    createRun: (f, t) => base.createRun(f, t),
    streamEvents: (id, on, signal) => base.streamEvents(id, on, signal),
    getTransforms: (id) => base.getTransforms(id),
    getPreview: async () => preview,
    getInput: (id, p) => base.getInput(id, p),
    getEvalSummary: () => base.getEvalSummary(),
  };
}

describe("side panel tabs", () => {
  test("Data, Code and Plan tabs are keyboard-accessible", async () => {
    const user = userEvent.setup();
    renderPanel("run-repaired", stateOf(RUN_REPAIRED_EVENTS));
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(tabs).toEqual(["Data", "Code", "Plan"]);
    const data = screen.getByRole("tab", { name: "Data" });
    expect(data).toHaveAttribute("aria-selected", "true");
    await user.click(data);
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Code" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Code");
  });
});

describe("data tab", () => {
  test("data tab virtualises a large preview", async () => {
    const { container } = renderPanel(
      "x",
      null,
      withPreview(bigPreview(10_000)),
    );
    const table = await screen.findByRole("table", {
      name: /before and after/i,
    });
    await waitFor(() =>
      expect(within(table).getAllByRole("row").length).toBeGreaterThan(5),
    );
    // Native <tr> elements carry an implicit row role, so count both the
    // accessible rows and the DOM rows (visible window + two spacers).
    expect(within(table).getAllByRole("row").length).toBeLessThan(200);
    expect(container.querySelectorAll("tr").length).toBeLessThan(200);
    // The accessible row count still reports every (changed) cell + header.
    expect(Number(table.getAttribute("aria-rowcount"))).toBeGreaterThan(10_000);
  });

  test("changed `before` values are struck through; 'Changed only' toggles unchanged cells", async () => {
    const user = userEvent.setup();
    renderPanel(
      "x",
      null,
      withPreview({
        columns: ["c"],
        totalRows: 2,
        nextOffset: null,
        rows: [
          {
            pk: "1",
            quarantined: false,
            reason: null,
            cells: { c: { before: "1,000", after: "1000", flagged: false } },
          },
          {
            pk: "2",
            quarantined: false,
            reason: null,
            cells: { c: { before: "5", after: "5", flagged: false } },
          },
        ],
      }),
    );
    const table = await screen.findByRole("table", {
      name: /before and after/i,
    });
    const struck = await within(table).findByText("1,000");
    expect(struck.tagName).toBe("DEL");
    expect(within(table).queryByText("5")).toBeNull();
    const toggle = screen.getByRole("switch", { name: "Changed only" });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    expect(toggle).not.toBeChecked();
    expect(within(table).getAllByText("5")).toHaveLength(2);
    expect(within(table).getAllByText("5")[0]?.tagName).not.toBe("DEL");
  });

  test("the focusable scroll box is a named region with no empty spacer rows", async () => {
    const user = userEvent.setup();
    renderPanel("run-repaired", stateOf(RUN_REPAIRED_EVENTS));
    const region = await screen.findByRole("region", {
      name: "Before and after values, scrollable",
    });
    const table = within(region).getByRole("table", {
      name: /before and after/i,
    });
    await waitFor(() =>
      expect(within(table).getAllByRole("row").length).toBeGreaterThan(5),
    );
    // Reachable by keyboard.
    screen.getByRole("switch", { name: "Changed only" }).focus();
    await user.tab();
    expect(region).toHaveFocus();
    // Off-window rows are margin, not empty spacer rows: every body row is
    // a real, indexed row.
    const bodyRows = [...table.querySelectorAll("tbody tr")];
    expect(bodyRows.length).toBeGreaterThan(5);
    expect(bodyRows.every((tr) => tr.hasAttribute("aria-rowindex"))).toBe(true);
  });

  test("the table's margins stand in exactly for the off-window rows", async () => {
    renderPanel("x", null, withPreview(bigPreview(10_000)));
    const region = await screen.findByRole("region", {
      name: "Before and after values, scrollable",
    });
    const table = within(region).getByRole("table");
    await waitFor(() =>
      expect(table.querySelectorAll("tbody tr").length).toBeGreaterThan(5),
    );
    // Every data row is DATA_ROW_HEIGHT px; aria-rowcount includes the header.
    const total =
      (Number(table.getAttribute("aria-rowcount")) - 1) * DATA_ROW_HEIGHT;
    const px = (v: string) => Number.parseFloat(v);

    const expectExactCover = () => {
      const rows = [...table.querySelectorAll("tbody tr")];
      // aria-rowindex is 1-based and the header is row 1.
      const firstIndex = Number(rows[0]?.getAttribute("aria-rowindex")) - 2;
      const top = px(table.style.marginTop);
      const bottom = px(table.style.marginBottom);
      expect(top).toBe(firstIndex * DATA_ROW_HEIGHT);
      expect(
        Math.abs(top + rows.length * DATA_ROW_HEIGHT + bottom - total),
      ).toBeLessThanOrEqual(1);
      return firstIndex;
    };

    expect(expectExactCover()).toBe(0);

    // jsdom has no scrolling: fake the offset, then fire the scroll event
    // the virtualiser listens for.
    let scrollTop = 0;
    Object.defineProperty(region, "scrollTop", {
      configurable: true,
      get: () => scrollTop,
      set: (v: number) => {
        scrollTop = v;
      },
    });
    act(() => {
      scrollTop = Math.round(total / 2);
      fireEvent.scroll(region);
    });
    await waitFor(() =>
      expect(
        Number(table.querySelector("tbody tr")?.getAttribute("aria-rowindex")),
      ).toBeGreaterThan(1_000),
    );
    expect(expectExactCover()).toBeGreaterThan(1_000);
  });

  test("flagged cell shows pill", async () => {
    renderPanel(
      "x",
      null,
      withPreview({
        columns: ["o_totalprice"],
        totalRows: 1,
        nextOffset: null,
        rows: [
          {
            pk: "42",
            quarantined: false,
            reason: null,
            cells: {
              o_totalprice: { before: "N/A", after: null, flagged: true },
            },
          },
        ],
      }),
    );
    const table = await screen.findByRole("table", {
      name: /before and after/i,
    });
    const row = (await within(table).findByText("42")).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("flagged")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("null")).toBeInTheDocument();
  });

  test("pages through the preview as the window needs rows", async () => {
    const base = new MockApiClient({ delayMs: 0 });
    const getPreview = vi.fn<ApiClient["getPreview"]>((id, page) =>
      base.getPreview(id, page),
    );
    const client: ApiClient = {
      ...withPreview(bigPreview(1)),
      getPreview,
    };
    renderPanel("run-repaired", stateOf(RUN_REPAIRED_EVENTS), client);
    const region = await screen.findByRole("region", {
      name: "Before and after values, scrollable",
    });
    const table = within(region).getByRole("table");
    expect(getPreview).toHaveBeenCalledTimes(1);
    expect(getPreview.mock.calls[0]?.[1]).toEqual({ offset: 0, limit: 1_000 });
    // More pages exist, so the full count is not known yet.
    expect(table).toHaveAttribute("aria-rowcount", "-1");
    // Rows shown of the upload, then rows loaded.
    expect(
      screen.getByText(
        /Showing 1,000 of 48,210 uploaded rows · 48,210 loaded$/,
      ),
    ).toBeInTheDocument();
    // Each body row in the DOM is one of the loaded rows; still windowed.
    expect(table.querySelectorAll("tr").length).toBeLessThan(200);

    // Scroll to the end of what is loaded: the next page is requested.
    let scrollTop = 0;
    Object.defineProperty(region, "scrollTop", {
      configurable: true,
      get: () => scrollTop,
      set: (v: number) => {
        scrollTop = v;
      },
    });
    act(() => {
      scrollTop = 1_000_000;
      fireEvent.scroll(region);
    });
    await waitFor(() => expect(getPreview).toHaveBeenCalledTimes(2));
    expect(getPreview.mock.calls[1]?.[1]).toEqual({
      offset: 1_000,
      limit: 1_000,
    });
    expect(
      await screen.findByText(
        /Showing 2,000 of 48,210 uploaded rows · 48,210 loaded$/,
      ),
    ).toBeInTheDocument();
    expect(table.querySelectorAll("tr").length).toBeLessThan(200);
  });

  test("quarantined rows show a pill with the reason", async () => {
    renderPanel(
      "x",
      null,
      withPreview({
        columns: ["l_orderkey"],
        totalRows: 1,
        nextOffset: null,
        rows: [
          {
            pk: "7|2",
            quarantined: true,
            reason: "l_orderkey 7 is not in orders",
            cells: { l_orderkey: { before: "7", after: "7", flagged: false } },
          },
        ],
      }),
    );
    const table = await screen.findByRole("table", {
      name: /before and after/i,
    });
    const row = (await within(table).findByText("7|2")).closest("tr");
    const pill = within(row as HTMLElement).getByText("quarantined");
    expect(pill).toHaveAttribute("title", "l_orderkey 7 is not in orders");
    const caption = document.getElementById(
      table.getAttribute("aria-describedby") ?? "",
    );
    expect(caption).toHaveTextContent(/1 quarantined/);
  });

  test("before the data has loaded the preview is not requested", async () => {
    const getPreview = vi.fn<ApiClient["getPreview"]>();
    const client = { ...withPreview(bigPreview(1)), getPreview };
    const loadedAt = RUN_REPAIRED_EVENTS.findIndex((e) => e.type === "loaded");
    renderPanel(
      "run-repaired",
      stateOf(RUN_REPAIRED_EVENTS.slice(0, loadedAt)),
      client,
    );
    expect(
      await screen.findByText(/preview appears once the data has loaded/i),
    ).toBeInTheDocument();
    expect(getPreview).not.toHaveBeenCalled();
  });

  test("a run that finished without loading has no preview", async () => {
    const getPreview = vi.fn<ApiClient["getPreview"]>();
    const client = { ...withPreview(bigPreview(1)), getPreview };
    const failed = stateOf([
      ...RUN_REPAIRED_EVENTS.slice(0, 6),
      {
        seq: 7,
        at: "2026-10-27T10:14:09.000Z",
        type: "run_finished",
        status: "failed",
        summary: "Gave up",
        costUsd: 0.0011,
        tokens: 1_480,
      },
    ]);
    renderPanel("run-repaired", failed, client);
    expect(
      await screen.findByText(/didn.t load any data/i),
    ).toBeInTheDocument();
    expect(getPreview).not.toHaveBeenCalled();
  });

  test("an empty preview says so", async () => {
    renderPanel(
      "x",
      null,
      withPreview({ columns: [], totalRows: 0, rows: [], nextOffset: null }),
    );
    expect(await screen.findByText(/no changed cells/i)).toBeInTheDocument();
  });

  test("a preview that is still being written (409 not_ready) is retried", async () => {
    const notReady = new ApiError(409, "/runs/x/preview", "409 Not ready", {
      detail: "The preview is still being written; try again shortly.",
      code: "not_ready",
    });
    const getPreview = vi
      .fn<ApiClient["getPreview"]>()
      .mockRejectedValueOnce(notReady)
      .mockResolvedValue(bigPreview(1));
    renderPanel("x", null, { ...withPreview(bigPreview(1)), getPreview });
    expect(
      await screen.findByRole("table", { name: "Before and after values" }),
    ).toBeInTheDocument();
    expect(getPreview).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  describe("not_ready retries stop when the panel goes away", () => {
    const notReady = () =>
      new ApiError(409, "/runs/x/preview", "409 Not ready", {
        code: "not_ready",
      });

    test("on unmount (panel closed)", async () => {
      vi.useFakeTimers();
      try {
        const getPreview = vi
          .fn<ApiClient["getPreview"]>()
          .mockImplementation(() => Promise.reject(notReady()));
        const { unmount } = renderPanel("x", null, {
          ...withPreview(bigPreview(1)),
          getPreview,
        });
        await act(() => vi.advanceTimersByTimeAsync(300));
        expect(getPreview).toHaveBeenCalledTimes(2);
        unmount();
        await act(() => vi.advanceTimersByTimeAsync(30_000));
        expect(getPreview).toHaveBeenCalledTimes(2);
      } finally {
        vi.useRealTimers();
      }
    });

    test("when the run changes", async () => {
      vi.useFakeTimers();
      try {
        const getPreview = vi
          .fn<ApiClient["getPreview"]>()
          .mockImplementation(() => Promise.reject(notReady()));
        const client = { ...withPreview(bigPreview(1)), getPreview };
        const { rerender } = renderPanel("run-a", null, client);
        await act(() => vi.advanceTimersByTimeAsync(0));
        rerender(
          <ApiProvider client={client}>
            <SidePanel runId="run-b" state={null} dataViewportHeight={480} />
          </ApiProvider>,
        );
        await act(() => vi.advanceTimersByTimeAsync(30_000));
        const runs = getPreview.mock.calls.map(([id]) => id);
        expect(runs.filter((id) => id === "run-a")).toHaveLength(1);
        expect(runs.filter((id) => id === "run-b").length).toBeGreaterThan(1);
      } finally {
        vi.useRealTimers();
      }
    });
  });

  test("a run without a stored preview (409 no_preview) is not retried", async () => {
    const getPreview = vi.fn<ApiClient["getPreview"]>().mockRejectedValue(
      new ApiError(409, "/runs/x/preview", "409 No preview", {
        detail: "This run has no preview.",
        code: "no_preview",
      }),
    );
    renderPanel("x", null, { ...withPreview(bigPreview(1)), getPreview });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This run has no preview.");
    expect(getPreview).toHaveBeenCalledTimes(1);
  });

  test("a failed preview offers a retry", async () => {
    const user = userEvent.setup();
    const client = withPreview({
      columns: [],
      totalRows: 0,
      rows: [],
      nextOffset: null,
    });
    const getPreview = vi
      .fn<ApiClient["getPreview"]>()
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue({
        columns: [],
        totalRows: 0,
        rows: [],
        nextOffset: null,
      });
    renderPanel("x", null, { ...client, getPreview });
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/no changed cells/i)).toBeInTheDocument();
    expect(getPreview).toHaveBeenCalledTimes(2);
  });
});

describe("code tab", () => {
  test("code tab shows attempts and diff", async () => {
    const user = userEvent.setup();
    const { container } = renderPanel(
      "run-repaired",
      stateOf(RUN_REPAIRED_EVENTS),
    );
    await user.click(screen.getByRole("tab", { name: "Code" }));
    const n1 = await screen.findByRole("region", {
      name: /normalise amounts/i,
    });
    const attempt2 = within(n1).getByRole("radio", { name: /attempt 2/i });
    expect(attempt2).toBeChecked();
    expect(
      within(n1).getByRole("radio", { name: /attempt 1/i }),
    ).not.toBeChecked();
    expect(within(n1).getByText(/failed/i)).toBeInTheDocument();
    await user.click(
      within(n1).getByRole("switch", { name: "Diff vs previous attempt" }),
    );
    expect(
      container.querySelectorAll('[data-kind="add"]').length,
    ).toBeGreaterThanOrEqual(1);
    expect(
      container.querySelectorAll('[data-kind="del"]').length,
    ).toBeGreaterThanOrEqual(1);
    // Single-attempt nodes have no diff toggle.
    const n2 = screen.getByRole("region", { name: /flag implausible/i });
    expect(within(n2).queryByRole("switch")).toBeNull();
  });

  test("picking an attempt shows its code", async () => {
    const user = userEvent.setup();
    renderPanel("run-repaired", stateOf(RUN_REPAIRED_EVENTS));
    await user.click(screen.getByRole("tab", { name: "Code" }));
    const n1 = await screen.findByRole("region", {
      name: /normalise amounts/i,
    });
    expect(within(n1).getByText(/Paise values/)).toBeInTheDocument();
    await user.click(within(n1).getByRole("radio", { name: /attempt 1/i }));
    expect(within(n1).queryByText(/Paise values/)).toBeNull();
  });

  test("a run with no transforms says so", async () => {
    const user = userEvent.setup();
    renderPanel("run-clean", stateOf(RUN_CLEAN_EVENTS));
    await user.click(screen.getByRole("tab", { name: "Code" }));
    expect(
      await screen.findByText(/deterministic rules handled this run/i),
    ).toBeInTheDocument();
  });

  test("a failed run without transforms does not credit the rules", async () => {
    const user = userEvent.setup();
    const empty = withPreview(bigPreview(1));
    const client: ApiClient = { ...empty, getTransforms: async () => [] };
    const failed = stateOf([
      ...RUN_REPAIRED_EVENTS.slice(0, 5),
      {
        seq: 6,
        at: "2026-10-27T10:14:09.000Z",
        type: "run_finished",
        status: "failed",
        summary: "Model provider unavailable",
        costUsd: 0,
        tokens: 0,
      },
    ]);
    renderPanel("run-repaired", failed, client);
    await user.click(screen.getByRole("tab", { name: "Code" }));
    expect(
      await screen.findByText(/run failed before any transform was written/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/deterministic rules/i)).toBeNull();
  });

  test("a finished run with plan nodes but no transforms is not 'rules only'", async () => {
    const user = userEvent.setup();
    const client: ApiClient = {
      ...withPreview(bigPreview(1)),
      getTransforms: async () => [],
    };
    renderPanel("run-repaired", stateOf(RUN_REPAIRED_EVENTS), client);
    await user.click(screen.getByRole("tab", { name: "Code" }));
    expect(
      await screen.findByText(/no generated code for this run/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/deterministic rules/i)).toBeNull();
  });

  test("refetches transforms once per reported attempt, not per status change", async () => {
    const user = userEvent.setup();
    const base = new MockApiClient({ delayMs: 0 });
    const getTransforms = vi.fn<ApiClient["getTransforms"]>((id) =>
      base.getTransforms(id),
    );
    const client: ApiClient = {
      ...withPreview({ columns: [], totalRows: 0, rows: [], nextOffset: null }),
      getTransforms,
    };
    let state = INITIAL_RUN_VIEW_STATE;
    const view = (s: RunViewState) => (
      <ApiProvider client={client}>
        <SidePanel runId="run-repaired" state={s} dataViewportHeight={480} />
      </ApiProvider>
    );
    const { rerender } = render(view(state));
    await user.click(screen.getByRole("tab", { name: "Code" }));
    await waitFor(() => expect(getTransforms).toHaveBeenCalledTimes(1));
    for (const e of RUN_REPAIRED_EVENTS) {
      state = reduceRunEvents(state, e);
      rerender(view(state));
      // Let each fetch settle before the next event, like the live stream.
      await screen.findByRole("tabpanel");
    }
    const reported = RUN_REPAIRED_EVENTS.filter(
      (e) => e.type === "attempt_failed" || e.type === "node_finished",
    ).length;
    await waitFor(() =>
      expect(getTransforms).toHaveBeenCalledTimes(1 + reported),
    );
    expect(
      await screen.findByRole("region", { name: /normalise amounts/i }),
    ).toBeInTheDocument();
  });

  test("a failed attempt's code shows as soon as it is reported", async () => {
    const user = userEvent.setup();
    const failed = RUN_REPAIRED_EVENTS.find((e) => e.type === "attempt_failed");
    const early = RUN_REPAIRED_EVENTS.filter(
      (e) => e.seq <= (failed?.seq ?? 0),
    );
    renderPanel("run-repaired", stateOf(early));
    await user.click(screen.getByRole("tab", { name: "Code" }));
    const n1 = await screen.findByRole("region", {
      name: /normalise amounts/i,
    });
    expect(within(n1).getAllByRole("radio")).toHaveLength(1);
    expect(within(n1).getByRole("radio", { name: /attempt 1/i })).toBeChecked();
  });

  test("hides transforms the live stream has not reported yet", async () => {
    const user = userEvent.setup();
    // Through node_started n1 attempt 1: nothing has been produced.
    const firstStart = RUN_REPAIRED_EVENTS.find(
      (e) => e.type === "node_started",
    );
    const early = RUN_REPAIRED_EVENTS.filter(
      (e) => e.seq <= (firstStart?.seq ?? 0),
    );
    renderPanel("run-repaired", stateOf(early));
    await user.click(screen.getByRole("tab", { name: "Code" }));
    expect(
      await screen.findByText(/no generated code yet/i),
    ).toBeInTheDocument();
  });
});

describe("plan tab", () => {
  test("plan tab layers nodes by dependency", async () => {
    const user = userEvent.setup();
    const { container } = renderPanel(
      "run-repaired",
      stateOf(RUN_REPAIRED_EVENTS),
    );
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    const n1 = container.querySelector('[data-node-id="n1"]');
    const n2 = container.querySelector('[data-node-id="n2"]');
    expect(n1).toHaveAttribute("data-layer", "0");
    expect(n2).toHaveAttribute("data-layer", "1");
    expect(n1).toHaveAttribute("data-status", "done");
    expect(container.querySelectorAll("[data-edge]")).toHaveLength(1);
    // Text alternative: the steps and their dependencies, in order.
    const steps = screen.getByRole("list", { name: /plan steps/i });
    const items = within(steps).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[1]).toHaveTextContent(/after step 1/i);
  });

  test("plan tab empty state", async () => {
    const user = userEvent.setup();
    renderPanel("run-clean", stateOf(RUN_CLEAN_EVENTS));
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    expect(
      screen.getByText("Rules fixed everything — no plan needed"),
    ).toBeInTheDocument();
  });

  test("before the plan arrives it says so", async () => {
    const user = userEvent.setup();
    renderPanel("run-repaired", stateOf(RUN_REPAIRED_EVENTS.slice(0, 1)));
    await user.click(screen.getByRole("tab", { name: "Plan" }));
    expect(screen.getByText(/planning/i)).toBeInTheDocument();
  });
});
