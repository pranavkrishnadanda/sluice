import { render, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { PlanTab } from "@/components/panel/PlanTab";
import type { RunEvent } from "@/lib/api/types";
import {
  INITIAL_RUN_VIEW_STATE,
  type RunViewState,
  reduceRunEvents,
} from "@/lib/run-state";

const AT = "2026-10-29T09:00:00.000Z";
type Body = RunEvent extends infer E
  ? E extends RunEvent
    ? Omit<E, "seq" | "at">
    : never
  : never;
const stateOf = (...bodies: Body[]): RunViewState =>
  bodies
    .map((b, i) => ({ ...b, seq: i + 1, at: AT }) as RunEvent)
    .reduce<RunViewState>(reduceRunEvents, INITIAL_RUN_VIEW_STATE);

const LONG =
  "Values appear to be paise with Indian digit grouping; need to remove commas and divide by 100 to obtain INR.";

/** Three parallel nodes and one that joins them (the owner's real shape). */
const events: Body[] = [
  {
    type: "run_started",
    runId: "r",
    fileName: "f.csv",
    table: "lineitem",
    rows: 10,
  },
  { type: "profiled", issues: [] },
  { type: "rules_applied", actions: [] },
  {
    type: "plan_created",
    nodes: [
      {
        id: "n1",
        title: LONG,
        columns: ["l_extendedprice"],
        tier: "small",
        dependsOn: [],
      },
      {
        id: "n2",
        title: "Fix ship dates",
        columns: ["l_shipdate"],
        tier: "small",
        dependsOn: [],
      },
      {
        id: "n3",
        title: "Fix receipt dates",
        columns: ["l_receiptdate", "l_commitdate"],
        tier: "small",
        dependsOn: [],
      },
      {
        id: "n4",
        title: "Check date order",
        columns: ["l_shipdate"],
        tier: "small",
        dependsOn: ["n2", "n3"],
      },
    ],
  },
  {
    type: "node_started",
    nodeId: "n1",
    attempt: 1,
    maxAttempts: 3,
    tier: "small",
  },
  {
    type: "attempt_failed",
    nodeId: "n1",
    attempt: 1,
    maxAttempts: 3,
    error: "bad",
    tokens: 1,
    costUsd: 0.001,
  },
  {
    type: "node_started",
    nodeId: "n1",
    attempt: 2,
    maxAttempts: 3,
    tier: "strong",
  },
  {
    type: "node_finished",
    nodeId: "n1",
    attempt: 2,
    tier: "strong",
    tokens: 2,
    costUsd: 0.0018,
    latencyMs: 7_300,
    transformId: "t1",
  },
  {
    type: "node_started",
    nodeId: "n2",
    attempt: 1,
    maxAttempts: 3,
    tier: "small",
  },
  {
    type: "activity",
    nodeId: "n2",
    phase: "writing",
    message: "Asking the small model to write a fix for l_shipdate",
    tier: "small",
    attempt: 1,
    retryInS: null,
  },
];

describe("PlanTab", () => {
  test("stacks dependency waves top to bottom as labelled lists", () => {
    render(<PlanTab state={stateOf(...events)} live />);
    const plan = screen.getByRole("region", { name: "Plan steps" });
    const headings = within(plan)
      .getAllByRole("heading")
      .map((h) => h.textContent);
    expect(headings).toEqual(["Runs in parallel · 3 steps", "Step"]);
    const lists = within(plan).getAllByRole("list");
    expect(lists).toHaveLength(2);
    expect(lists[0]).toHaveAccessibleName("Runs in parallel · 3 steps");
    expect(
      within(lists[0] as HTMLElement).getAllByRole("listitem"),
    ).toHaveLength(3);
    expect(within(plan).getAllByRole("listitem")).toHaveLength(4);
  });

  test("each card shows its step, columns, full title, chips and meta", () => {
    render(<PlanTab state={stateOf(...events)} live />);
    const n1 = screen.getByTestId("plan-node-n1");
    expect(n1).toHaveTextContent("1");
    expect(within(n1).getByText("l_extendedprice")).toHaveClass("break-all");
    // The whole title, wrapped, never clipped; cost sums every attempt.
    expect(within(n1).getByText(LONG)).toBeInTheDocument();
    expect(n1).not.toHaveTextContent("…");
    expect(within(n1).getByText("strong")).toBeInTheDocument();
    expect(within(n1).getByText("done")).toBeInTheDocument();
    expect(n1).toHaveTextContent(
      "2 attempts · retried on strong · 7.3 s · $0.0028",
    );

    const n3 = screen.getByTestId("plan-node-n3");
    expect(
      within(n3).getByText("l_receiptdate, l_commitdate"),
    ).toBeInTheDocument();
    expect(within(n3).getByText("pending")).toBeInTheDocument();

    const n4 = screen.getByTestId("plan-node-n4");
    expect(n4).toHaveTextContent("depends on: 2, 3");
  });

  test("a running node shows its live activity line inside the card", () => {
    render(<PlanTab state={stateOf(...events)} live />);
    const n2 = screen.getByTestId("plan-node-n2");
    expect(within(n2).getByText("running")).toBeInTheDocument();
    expect(within(n2).getByTestId("activity-line")).toHaveTextContent(
      "Asking the small model to write a fix for l_shipdate",
    );
    expect(screen.getAllByTestId("activity-line")).toHaveLength(1);
  });

  test("no activity line once the stream is not live", () => {
    render(<PlanTab state={stateOf(...events)} />);
    expect(screen.queryByTestId("activity-line")).not.toBeInTheDocument();
  });

  test("a node cut off by the run's end reads as stopped", () => {
    const state = stateOf(...events, {
      type: "run_finished",
      status: "failed",
      summary: "cap",
      costUsd: 0,
      tokens: 0,
    });
    render(<PlanTab state={state} />);
    expect(
      within(screen.getByTestId("plan-node-n2")).getByText("stopped"),
    ).toBeInTheDocument();
  });

  test("nothing in the plan scrolls sideways at 360 px", () => {
    // jsdom has no layout, so this guards the classes that make wrapping
    // possible; e2e/responsive.spec.ts measures real overflow in Chrome.
    const { container } = render(
      <div style={{ width: 360 }}>
        <PlanTab state={stateOf(...events)} live />
      </div>,
    );
    // No SVG-drawn text (the old fixed-width graph).
    expect(container.querySelector("svg text")).toBeNull();
    expect(container.querySelector(".overflow-x-auto")).toBeNull();
    expect(
      container.querySelector(".truncate, .whitespace-nowrap.flex-1"),
    ).toBeNull();
    for (const el of container.querySelectorAll<HTMLElement>("*")) {
      expect(el.scrollWidth).toBeLessThanOrEqual(el.clientWidth);
    }
  });
});
