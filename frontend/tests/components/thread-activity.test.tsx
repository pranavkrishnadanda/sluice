import { act, render, screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { ApiProvider } from "@/components/api/ApiProvider";
import { RunThread } from "@/components/thread/RunThread";
import { RUN_REPAIRED_LIVE_EVENTS } from "@/lib/api/fixtures/run-repaired-live";
import { MockApiClient } from "@/lib/api/mock-client";
import type { ApiClient, RunEvent } from "@/lib/api/types";
import { rememberAutoTable } from "@/lib/auto-table";

vi.mock("next/navigation", () => ({
  usePathname: () => "/runs",
  useRouter: () => ({ push: vi.fn<(href: string) => void>() }),
}));

/** A client whose stream the test drives event by event. */
function driven(events: readonly RunEvent[] = RUN_REPAIRED_LIVE_EVENTS) {
  const base = new MockApiClient({ delayMs: 0 });
  let push: ((e: RunEvent) => void) | null = null;
  const client: ApiClient = {
    isDemo: false,
    listRuns: () => base.listRuns(),
    createRun: (f, t) => base.createRun(f, t),
    streamEvents: (_id, onEvent) =>
      new Promise<void>(() => {
        push = onEvent;
      }),
    getTransforms: (id) => base.getTransforms(id),
    getPreview: (id, p) => base.getPreview(id, p),
    getInput: (id, p) => base.getInput(id, p),
    getEvalSummary: () => base.getEvalSummary(),
  };
  /** Delivers every event up to and including the first that matches. */
  const until = (match: (e: RunEvent) => boolean) => {
    const end = events.findIndex(match);
    act(() => {
      for (const e of events.slice(0, end + 1)) push?.(structuredClone(e));
    });
  };
  return { client, until };
}

const nodeRow = (title: string) =>
  within(screen.getByText(title).closest("li") as HTMLElement);

const N1 = "Map amt to o_totalprice and normalise amounts";

describe("live activity in the thread", () => {
  test("a run-wide phase shows under the in-flight milestone", () => {
    const { client, until } = driven();
    render(
      <ApiProvider client={client}>
        <RunThread runId="run-repaired" />
      </ApiProvider>,
    );
    until((e) => e.type === "activity" && e.phase === "planning");
    const row = within(
      screen.getByText("Planning repairs…").closest("li") as HTMLElement,
    );
    expect(row.getByTestId("activity-line")).toHaveTextContent(
      "Asking the small model to plan the repairs",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      /Now: Asking the small model to plan the repairs\.$/,
    );
  });

  test("a node phase shows on its node; the next phase replaces it", () => {
    const { client, until } = driven();
    render(
      <ApiProvider client={client}>
        <RunThread runId="run-repaired" />
      </ApiProvider>,
    );
    until((e) => e.type === "activity" && e.phase === "writing");
    expect(nodeRow(N1).getByTestId("activity-line")).toHaveTextContent(
      "Asking the small model to write a fix for amt",
    );
    until((e) => e.type === "activity" && e.phase === "sandbox");
    const lines = screen.getAllByTestId("activity-line");
    expect(lines).toHaveLength(1);
    expect(lines[0]).toHaveTextContent(/Running the fix in the sandbox/);
  });

  test("a back-off counts down on the retrying node", () => {
    const { client, until } = driven();
    render(
      <ApiProvider client={client}>
        <RunThread runId="run-repaired" />
      </ApiProvider>,
    );
    until((e) => e.type === "activity" && e.phase === "waiting");
    const line = nodeRow(N1).getByTestId("activity-line");
    expect(line).toHaveAttribute("data-phase", "waiting");
    expect(line).toHaveTextContent(/Rate limit reached on the strong model/);
    expect(line).toHaveTextContent(/retrying (in \d s|now)/);
  });

  test("a retry shows the tier its attempt runs on", () => {
    const { client, until } = driven();
    render(
      <ApiProvider client={client}>
        <RunThread runId="run-repaired" />
      </ApiProvider>,
    );
    until((e) => e.type === "node_started" && e.nodeId === "n1");
    expect(nodeRow(N1).getByText("small")).toBeInTheDocument();
    until(
      (e) => e.type === "node_started" && e.nodeId === "n1" && e.attempt === 2,
    );
    expect(nodeRow(N1).getByText("strong")).toBeInTheDocument();
    expect(nodeRow(N1).queryByText("small")).not.toBeInTheDocument();
  });

  test("run_finished clears every activity line", () => {
    const { client, until } = driven();
    render(
      <ApiProvider client={client}>
        <RunThread runId="run-repaired" />
      </ApiProvider>,
    );
    until((e) => e.type === "run_finished");
    expect(screen.queryByTestId("activity-line")).not.toBeInTheDocument();
  });
});

describe("resolved table", () => {
  test("a run started on Auto says which table was matched", async () => {
    const { client, until } = driven();
    rememberAutoTable("run-repaired");
    render(
      <ApiProvider client={client}>
        <RunThread runId="run-repaired" />
      </ApiProvider>,
    );
    until((e) => e.type === "run_started");
    expect(
      screen.getByText("Load this into the table it matches."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Matched the/)).toHaveTextContent(
      "Matched the orders table",
    );
    sessionStorage.clear();
  });
});
