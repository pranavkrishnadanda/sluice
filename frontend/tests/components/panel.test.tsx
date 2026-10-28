import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { ApiProvider } from "@/components/api/ApiProvider";
import { SidePanel } from "@/components/panel/SidePanel";
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

function withPreview(preview: DataPreview): ApiClient {
  const base = new MockApiClient({ delayMs: 0 });
  return {
    isDemo: true,
    listRuns: () => base.listRuns(),
    createRun: (f, t) => base.createRun(f, t),
    streamEvents: (id, on, signal) => base.streamEvents(id, on, signal),
    getTransforms: (id) => base.getTransforms(id),
    getPreview: async () => preview,
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
  test("data tab virtualises 50k rows", async () => {
    const { container } = renderPanel(
      "run-repaired",
      stateOf(RUN_REPAIRED_EVENTS),
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
        rows: [
          {
            pk: "1",
            cells: { c: { before: "1,000", after: "1000", flagged: false } },
          },
          {
            pk: "2",
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
    expect(table.style.marginBottom).not.toBe("0px");
  });

  test("flagged cell shows pill", async () => {
    renderPanel(
      "x",
      null,
      withPreview({
        columns: ["o_totalprice"],
        totalRows: 1,
        rows: [
          {
            pk: "42",
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

  test("an empty preview says so", async () => {
    renderPanel(
      "x",
      null,
      withPreview({ columns: [], totalRows: 0, rows: [] }),
    );
    expect(await screen.findByText(/no changed cells/i)).toBeInTheDocument();
  });

  test("a failed preview offers a retry", async () => {
    const user = userEvent.setup();
    const client = withPreview({ columns: [], totalRows: 0, rows: [] });
    const getPreview = vi
      .fn<ApiClient["getPreview"]>()
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue({ columns: [], totalRows: 0, rows: [] });
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
    expect(await screen.findByText(/no generated code/i)).toBeInTheDocument();
  });

  test("refetches transforms once per reported attempt, not per status change", async () => {
    const user = userEvent.setup();
    const base = new MockApiClient({ delayMs: 0 });
    const getTransforms = vi.fn<ApiClient["getTransforms"]>((id) =>
      base.getTransforms(id),
    );
    const client: ApiClient = {
      ...withPreview({ columns: [], totalRows: 0, rows: [] }),
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
