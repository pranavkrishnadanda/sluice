import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ApiProvider } from "@/components/api/ApiProvider";
import { AppShell } from "@/components/shell/AppShell";
import { groupRunsByDay } from "@/components/shell/group-runs";
import { RunStreamProvider } from "@/components/thread/run-stream";
import { MockApiClient } from "@/lib/api/mock-client";
import type { ApiClient, RunEvent, RunSummary } from "@/lib/api/types";
import { setViewportWidth } from "../helpers/viewport";

const nav = vi.hoisted(() => ({ pathname: "/runs" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
}));

/** A non-demo client whose `listRuns` is controlled by the test. */
function stubClient(listRuns: () => Promise<RunSummary[]>): ApiClient {
  const never = () => Promise.reject(new Error("not used"));
  return {
    isDemo: false,
    listRuns,
    createRun: never,
    streamEvents: never,
    getTransforms: never,
    getPreview: never,
    getEvalSummary: never,
  };
}

function renderShell(
  client: ApiClient = new MockApiClient({ delayMs: 0 }),
  props: { sidePanel?: ReactNode } = {},
) {
  return render(
    <ApiProvider client={client}>
      <AppShell {...props}>
        <h1>Page content</h1>
      </AppShell>
    </ApiProvider>,
  );
}

function sidebar() {
  return screen.getByRole("navigation", { name: "Primary", hidden: true });
}

beforeEach(() => {
  nav.pathname = "/runs";
  setViewportWidth(1280);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AppShell", () => {
  test("renders the Sluice brand, the page content and a skip link", () => {
    renderShell();
    expect(screen.getByRole("link", { name: "Sluice" })).toHaveAttribute(
      "href",
      "/runs",
    );
    expect(
      screen.getByRole("heading", { name: "Page content" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
    expect(
      screen.getByRole("link", { name: "Skip to content" }),
    ).toHaveAttribute("href", "#main");
    expect(screen.getByRole("button", { name: /^Theme:/ })).toBeInTheDocument();
  });

  test("sidebar lists the 3 fixture runs, linking to each run", async () => {
    renderShell();
    const list = within(sidebar());
    const clean = await list.findByRole("link", {
      name: /orders_2026-09\.csv/,
    });
    expect(clean).toHaveAttribute("href", "/runs/run-clean");
    expect(
      list.getByRole("link", { name: /orders_2026-10\.csv/ }),
    ).toHaveAttribute("href", "/runs/run-repaired");
    expect(
      list.getByRole("link", { name: /lineitem_2026-10\.csv/ }),
    ).toHaveAttribute("href", "/runs/run-quarantined");
    // Status is conveyed in text, not only by the dot's colour.
    expect(
      list.getByRole("link", { name: /lineitem_2026-10\.csv.*quarantined/i }),
    ).toBeInTheDocument();
  });

  test("the open run's status dot refreshes when its run_finished arrives", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const run = await client.createRun(
      new File(["a"], "fresh.csv", { type: "text/csv" }),
      "orders",
    );
    nav.pathname = `/runs/${run.id}`;
    const listRuns = vi.spyOn(client, "listRuns");
    let emit: ((e: RunEvent) => void) | null = null;
    let finish: (() => void) | null = null;
    const events: RunEvent[] = [];
    await client.streamEvents(
      run.id,
      (e) => events.push(e),
      new AbortController().signal,
    );
    vi.spyOn(client, "streamEvents").mockImplementation(
      (_id, onEvent) =>
        new Promise<void>((resolve) => {
          emit = onEvent;
          finish = resolve;
        }),
    );
    render(
      <ApiProvider client={client}>
        <RunStreamProvider runId={run.id}>
          <AppShell>
            <h1>Run</h1>
          </AppShell>
        </RunStreamProvider>
      </ApiProvider>,
    );
    await within(sidebar()).findByRole("link", { name: /fresh\.csv/ });
    const before = listRuns.mock.calls.length;
    act(() => {
      for (const e of events.slice(0, -1)) emit?.(e);
    });
    expect(listRuns.mock.calls.length).toBe(before);
    act(() => {
      const last = events.at(-1);
      if (last) emit?.(last);
      finish?.();
    });
    await waitFor(() => expect(listRuns.mock.calls.length).toBe(before + 1));
    expect(
      await within(sidebar()).findByRole("link", {
        name: /fresh\.csv, succeeded/i,
      }),
    ).toBeInTheDocument();
  });

  test("sidebar has New pipeline run and Evals links", async () => {
    renderShell();
    const list = within(sidebar());
    expect(
      list.getByRole("link", { name: "New pipeline run" }),
    ).toHaveAttribute("href", "/runs");
    expect(list.getByRole("link", { name: "Evals" })).toHaveAttribute(
      "href",
      "/evals",
    );
    await list.findByRole("link", { name: /orders_2026-09\.csv/ });
  });

  test("the active run link has aria-current=page", async () => {
    nav.pathname = "/runs/run-repaired";
    renderShell();
    const list = within(sidebar());
    const active = await list.findByRole("link", {
      name: /orders_2026-10\.csv/,
    });
    expect(active).toHaveAttribute("aria-current", "page");
    expect(
      list.getByRole("link", { name: /orders_2026-09\.csv/ }),
    ).not.toHaveAttribute("aria-current");
    expect(
      list.getByRole("link", { name: "New pipeline run" }),
    ).not.toHaveAttribute("aria-current");
  });

  test("New pipeline run is current on /runs", () => {
    renderShell();
    expect(
      within(sidebar()).getByRole("link", { name: "New pipeline run" }),
    ).toHaveAttribute("aria-current", "page");
  });

  test("DemoBadge shows with the mock client", () => {
    renderShell();
    expect(screen.getByText("Demo data")).toBeInTheDocument();
  });

  test("DemoBadge is absent with a non-demo client", async () => {
    renderShell(stubClient(async () => []));
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
    expect(screen.queryByText("Demo data")).not.toBeInTheDocument();
  });

  test("shows a loading state while runs load", () => {
    renderShell(stubClient(() => new Promise<RunSummary[]>(() => {})));
    expect(screen.getByRole("status")).toHaveTextContent("Loading runs");
  });

  test("shows an error with Retry when runs fail to load", async () => {
    const listRuns = vi
      .fn<() => Promise<RunSummary[]>>()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce([]);
    renderShell(stubClient(listRuns));
    expect(await screen.findByText("Couldn't load runs.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
    expect(listRuns).toHaveBeenCalledTimes(2);
  });

  test("at 600 px a menu button toggles the sidebar", async () => {
    setViewportWidth(600);
    const user = userEvent.setup();
    renderShell();
    const menu = screen.getByRole("button", { name: "Menu" });
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(menu).toHaveAttribute("aria-controls", "app-sidebar");
    await waitFor(() => expect(sidebar()).not.toBeVisible());

    await user.click(menu);
    expect(menu).toHaveAttribute("aria-expanded", "true");
    expect(sidebar()).toBeVisible();
    // Focus moves into the drawer and the page behind it is inert.
    expect(sidebar()).toContainElement(document.activeElement as HTMLElement);
    expect(screen.getByRole("main", { hidden: true })).toHaveAttribute("inert");

    await user.keyboard("{Escape}");
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(sidebar()).not.toBeVisible();
    expect(menu).toHaveFocus();
    expect(screen.getByRole("main")).not.toHaveAttribute("inert");

    await user.click(menu);
    expect(sidebar()).toBeVisible();
    await user.click(menu);
    expect(sidebar()).not.toBeVisible();
  });

  test("at 1280 px the sidebar is always visible", async () => {
    renderShell();
    expect(sidebar()).toBeVisible();
    await within(sidebar()).findByRole("link", { name: /orders_2026-09\.csv/ });
  });

  test("no Details button without a side panel", () => {
    setViewportWidth(900);
    renderShell();
    expect(
      screen.queryByRole("button", { name: "Details" }),
    ).not.toBeInTheDocument();
  });

  test("below 1024 px the side panel is a drawer behind Details", async () => {
    setViewportWidth(900);
    const user = userEvent.setup();
    renderShell(undefined, { sidePanel: <p>Panel body</p> });
    // A hidden element has no accessible name, so look it up by id.
    const panel = () => {
      const el = document.getElementById("app-details");
      if (!el) throw new Error("no side panel");
      return el;
    };
    const details = screen.getByRole("button", { name: "Details" });
    expect(details).toHaveAttribute("aria-expanded", "false");
    expect(details).toHaveAttribute("aria-controls", "app-details");
    await waitFor(() => expect(panel()).not.toBeVisible());
    // The sidebar is not collapsed at this width.
    expect(sidebar()).toBeVisible();

    await user.click(details);
    expect(details).toHaveAttribute("aria-expanded", "true");
    expect(panel()).toBeVisible();
    expect(
      screen.getByRole("complementary", { name: "Details" }),
    ).toContainElement(screen.getByText("Panel body"));
    expect(screen.getByRole("main", { hidden: true })).toHaveAttribute("inert");

    await user.keyboard("{Escape}");
    expect(panel()).not.toBeVisible();
    expect(details).toHaveFocus();
  });

  test("at 1280 px the side panel is shown beside the thread", () => {
    renderShell(undefined, { sidePanel: <p>Panel body</p> });
    expect(
      screen.getByRole("complementary", { name: "Details" }),
    ).toBeVisible();
  });
});

describe("groupRunsByDay", () => {
  const run = (id: string, createdAt: Date): RunSummary => ({
    id,
    fileName: `${id}.csv`,
    table: "orders",
    rows: 1,
    status: "succeeded",
    createdAt: createdAt.toISOString(),
    costUsd: 0,
  });

  test("groups by local day, newest first, dropping empty groups", () => {
    const now = new Date(2026, 9, 28, 9, 30);
    const groups = groupRunsByDay(
      [
        run("old", new Date(2026, 9, 20, 12)),
        run("today-early", new Date(2026, 9, 28, 0, 5)),
        run("yesterday", new Date(2026, 9, 27, 23, 59)),
        run("today-late", new Date(2026, 9, 28, 9, 0)),
      ],
      now,
    );
    expect(groups.map((g) => [g.label, g.runs.map((r) => r.id)])).toEqual([
      ["Today", ["today-late", "today-early"]],
      ["Yesterday", ["yesterday"]],
      ["Earlier", ["old"]],
    ]);
    expect(groupRunsByDay([], now)).toEqual([]);
  });
});
