import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import EvalsPage from "@/app/evals/page";
import { ApiProvider } from "@/components/api/ApiProvider";
import {
  type ChartColours,
  FALLBACK_COLOURS,
} from "@/components/evals/chart-colours";
import { EvalsView } from "@/components/evals/EvalsView";
import { ParetoChart } from "@/components/evals/ParetoChart";
import {
  buildParetoOption,
  paretoRows,
} from "@/components/evals/pareto-option";
import { ResultsTable } from "@/components/evals/ResultsTable";
import { EVAL_SUMMARY } from "@/lib/api/fixtures/eval-summary";
import { MockApiClient } from "@/lib/api/mock-client";
import type { ApiClient, EvalSummary } from "@/lib/api/types";
import { setViewportWidth } from "../helpers/viewport";

/* ECharts is mocked at its modular entry points: no rendering in jsdom. */
const ec = vi.hoisted(() => {
  const setOption = vi.fn<(option: Record<string, unknown>) => void>();
  const resize = vi.fn<() => void>();
  const dispose = vi.fn<() => void>();
  const init = vi.fn<
    () => {
      setOption: typeof setOption;
      resize: () => void;
      dispose: () => void;
    }
  >(() => ({ setOption, resize, dispose }));
  return { init, setOption, resize, dispose, use: vi.fn<() => void>() };
});
vi.mock("echarts/core", () => ({ init: ec.init, use: ec.use }));
vi.mock("echarts/charts", () => ({ ScatterChart: {}, LineChart: {} }));
vi.mock("echarts/components", () => ({
  GridComponent: {},
  TooltipComponent: {},
  LegendComponent: {},
}));
vi.mock("echarts/renderers", () => ({ SVGRenderer: {} }));

vi.mock("next/navigation", () => ({ usePathname: () => "/evals" }));

const resizeObservers: Array<{ cb: ResizeObserverCallback }> = [];
class FakeResizeObserver {
  cb: ResizeObserverCallback;
  constructor(cb: ResizeObserverCallback) {
    this.cb = cb;
    resizeObservers.push(this);
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  ec.init.mockClear();
  ec.setOption.mockClear();
  ec.resize.mockClear();
  ec.dispose.mockClear();
  resizeObservers.length = 0;
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  setViewportWidth(1280);
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("style");
});

const COLOURS: ChartColours = {
  ...FALLBACK_COLOURS,
  baseline: "#0f7f5c",
  routers: ["#2a63d6", "#c2501b"],
};

function xTickLabel(option: { xAxis?: unknown }): (v: number) => string {
  const axis = option.xAxis as {
    axisLabel: { formatter: (v: number) => string };
  };
  return axis.axisLabel.formatter;
}

function lastOption(): Record<string, unknown> {
  const call = ec.setOption.mock.calls.at(-1);
  if (!call) throw new Error("setOption was never called");
  return call[0];
}

function stubClient(getEvalSummary: () => Promise<EvalSummary>): ApiClient {
  const never = () => Promise.reject(new Error("not used"));
  return {
    isDemo: true,
    listRuns: async () => [],
    createRun: never,
    streamEvents: never,
    getTransforms: never,
    getPreview: never,
    getEvalSummary,
  };
}

describe("ResultsTable", () => {
  test("renders each metric with its 95% interval in brackets", () => {
    render(<ResultsTable summary={EVAL_SUMMARY} />);
    const row = screen.getByRole("row", { name: /sluice/ });
    expect(within(row).getByText("86.1% [84.2, 87.9]")).toBeInTheDocument();
    expect(within(row).getByText("1.2% [0.7, 1.9]")).toBeInTheDocument();
    expect(within(row).getByText("81.0% [76.0, 85.0]")).toBeInTheDocument();
    expect(within(row).getByText("97.5% [96.0, 98.6]")).toBeInTheDocument();
    expect(within(row).getByText("1.1 s [940 ms, 1.3 s]")).toBeInTheDocument();
    expect(within(row).getByText("$0.12 [$0.10, $0.15]")).toBeInTheDocument();
  });

  test("a null metric renders an em dash", () => {
    render(<ResultsTable summary={EVAL_SUMMARY} />);
    const row = screen.getByRole("row", { name: /^rules/ });
    const cells = within(row).getAllByRole("cell");
    // System is the row header; cells: accuracy, fabrication, contract
    // pass rate, pass^4, p95 latency, cost.
    expect(cells[3]).toHaveTextContent("—");
  });

  test("has contract pass rate and p95 latency columns", () => {
    render(<ResultsTable summary={EVAL_SUMMARY} />);
    const headers = screen
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(headers).toEqual([
      "System",
      "Cell accuracyhigher is better",
      "Fabrication ratelower is better",
      "Contract pass ratehigher is better",
      "pass^4higher is better",
      "p95 latencylower is better",
      "Cost / 1k rowslower is better",
    ]);
  });

  test("explains pass^4 as the tau-bench all-runs-succeed estimate", () => {
    render(<ResultsTable summary={EVAL_SUMMARY} />);
    expect(
      screen.getByText(
        /pass\^4 is the estimated probability that all 4 runs of a case succeed \(τ-bench pass\^k estimator\)/,
      ),
    ).toBeInTheDocument();
  });

  test("captions the table with split and n, one row per system", () => {
    render(<ResultsTable summary={EVAL_SUMMARY} />);
    const table = screen.getByRole("table");
    expect(table).toHaveAccessibleName(/holdout · n = 400/);
    // header row + 3 systems
    expect(within(table).getAllByRole("row")).toHaveLength(4);
  });
});

describe("buildParetoOption", () => {
  test("uses a log cost axis and a percentage quality axis", () => {
    const option = buildParetoOption(EVAL_SUMMARY, COLOURS);
    expect(option.xAxis).toMatchObject({ type: "log" });
    expect(option.yAxis).toMatchObject({ type: "value" });
  });

  test("baselines are labelled points, routers are lines carrying lambda", () => {
    const option = buildParetoOption(EVAL_SUMMARY, COLOURS);
    const series = option.series as Array<Record<string, unknown>>;
    const scatter = series.filter((s) => s.type === "scatter");
    const routers = series.filter((s) => s.type === "line");
    // One "Baselines" series, one direct-labelled point per baseline system.
    expect(scatter).toHaveLength(1);
    const baselines = scatter[0] as {
      label: unknown;
      itemStyle: unknown;
      data: Array<{ name: string }>;
    };
    expect(baselines.label).toMatchObject({ show: true });
    expect(baselines.itemStyle).toMatchObject({ color: "#0f7f5c" });
    expect(baselines.data.map((d) => d.name)).toEqual([
      "rules",
      "always-small",
      "always-strong",
    ]);
    expect(routers).toHaveLength(1);
    const router = routers[0] as { data: Array<{ lambda?: number }> };
    expect(router).toMatchObject({
      name: "sluice",
      itemStyle: { color: "#2a63d6" },
    });
    expect(router.data.map((d) => d.lambda)).toEqual([2, 1, 0.5, 0.25, 0.1]);
  });

  test("zero-cost points get their own decade below every real tick", () => {
    const option = buildParetoOption(EVAL_SUMMARY, COLOURS);
    // Cheapest paid point is $0.02, so real ticks start at $0.01; the zero
    // draw position is one decade lower.
    expect(option.xAxis).toMatchObject({ min: 0.001, max: 1 });
    const series = option.series as Array<{
      type: string;
      data: Array<{ name: string; value: [number, number]; cost: number }>;
    }>;
    const rules = series
      .find((s) => s.type === "scatter")
      ?.data.find((d) => d.name === "rules");
    expect(rules).toMatchObject({ value: [0.001, 0.712], cost: 0 });
    const label = xTickLabel(option);
    expect(label(0.001)).toBe("$0");
    expect(label(0.01)).toBe("$0.01");
    expect(label(0.1)).toBe("$0.10");
  });

  test("a real tick at the paid floor keeps its value when a zero is present", () => {
    const summary: EvalSummary = {
      ...EVAL_SUMMARY,
      pareto: [
        {
          system: "free",
          kind: "baseline",
          points: [{ costPer1kRowsUsd: 0, quality: 0.7, lambda: null }],
        },
        {
          system: "mid",
          kind: "baseline",
          points: [{ costPer1kRowsUsd: 0.5, quality: 0.8, lambda: null }],
        },
        {
          system: "dear",
          kind: "baseline",
          points: [{ costPer1kRowsUsd: 0.9, quality: 0.85, lambda: null }],
        },
      ],
    };
    const option = buildParetoOption(summary, COLOURS);
    expect(option.xAxis).toMatchObject({ min: 0.01, max: 1 });
    const label = xTickLabel(option);
    expect(label(0.1)).toBe("$0.10");
    expect(label(0.01)).toBe("$0");
    const free = (
      option.series as Array<{ data: Array<{ name: string; value: number[] }> }>
    )[0]?.data.find((d) => d.name === "free");
    expect(free?.value[0]).toBe(0.01);
  });

  test("without a zero-cost point no tick is relabelled", () => {
    const summary: EvalSummary = {
      ...EVAL_SUMMARY,
      pareto: [
        {
          system: "mid",
          kind: "baseline",
          points: [{ costPer1kRowsUsd: 0.5, quality: 0.8, lambda: null }],
        },
      ],
    };
    const option = buildParetoOption(summary, COLOURS);
    expect(option.xAxis).toMatchObject({ min: 0.1, max: 1 });
    expect(xTickLabel(option)(0.1)).toBe("$0.10");
  });

  test("tooltip text is HTML-escaped and states the real cost and lambda", () => {
    const option = buildParetoOption(EVAL_SUMMARY, COLOURS);
    const format = (option.tooltip as { formatter: (p: unknown) => string })
      .formatter;
    expect(
      format({
        data: { name: "<b>x</b>", value: [0.01, 0.7], cost: 0 },
        seriesType: "scatter",
      }),
    ).toContain("&lt;b&gt;x&lt;/b&gt;");
    const router = format({
      data: { name: "sluice", value: [0.12, 0.861], cost: 0.12, lambda: 0.5 },
      seriesType: "line",
    });
    expect(router).toContain("$0.12");
    expect(router).toContain("86.1%");
    expect(router).toContain("λ = 0.5");
  });

  test("a null lambda is treated as absent everywhere (never 'λ = null')", () => {
    const summary: EvalSummary = {
      ...EVAL_SUMMARY,
      pareto: [
        {
          system: "always-small",
          kind: "baseline",
          points: [{ costPer1kRowsUsd: 0.08, quality: 0.8, lambda: null }],
        },
        {
          system: "sluice",
          kind: "router",
          points: [
            { costPer1kRowsUsd: 0.05, quality: 0.82, lambda: null },
            { costPer1kRowsUsd: 0.12, quality: 0.86, lambda: 0.5 },
          ],
        },
      ],
    };
    const rows = paretoRows(summary);
    expect(rows.map((r) => r.lambda)).toEqual([null, null, 0.5]);
    const option = buildParetoOption(summary, COLOURS);
    const series = option.series as Array<{
      type: string;
      data: Array<{ lambda?: number | null }>;
    }>;
    const line = series.find((s) => s.type === "line");
    expect(line?.data.map((d) => d.lambda)).toEqual([undefined, 0.5]);
    const format = (option.tooltip as { formatter: (p: unknown) => string })
      .formatter;
    for (const s of series) {
      for (const d of s.data) {
        expect(format({ data: d, seriesType: s.type })).not.toContain("null");
      }
    }
    expect(
      format({
        data: { name: "x", value: [0.05, 0.82], cost: 0.05, lambda: null },
      }),
    ).not.toContain("λ");
  });

  test("animation is off under reduced motion", () => {
    expect(buildParetoOption(EVAL_SUMMARY, COLOURS).animation).toBe(true);
    expect(
      buildParetoOption(EVAL_SUMMARY, COLOURS, { reducedMotion: true })
        .animation,
    ).toBe(false);
  });
});

describe("ParetoChart", () => {
  test("the hidden table lists every point (3 baselines + 5 router)", () => {
    expect(paretoRows(EVAL_SUMMARY)).toHaveLength(8);
    render(<ParetoChart summary={EVAL_SUMMARY} />);
    const table = screen.getByRole("table", { name: /cost.*quality/i });
    const body = within(table).getAllByRole("rowgroup")[1];
    if (!body) throw new Error("no tbody");
    expect(within(body).getAllByRole("row")).toHaveLength(8);
    const half = within(body)
      .getAllByRole("row")
      .find((r) => within(r).queryByText("0.5"));
    expect(half).toHaveTextContent(/sluice.*router.*\$0\.12.*86\.1%/);
    const rules = within(body)
      .getAllByRole("row")
      .find((r) => within(r).queryByText("rules"));
    expect(rules).toHaveTextContent(/rules.*baseline.*\$0.*71\.2%/);
  });

  test("passes an option with a log x axis to setOption", async () => {
    render(<ParetoChart summary={EVAL_SUMMARY} />);
    await waitFor(() => expect(ec.setOption).toHaveBeenCalled());
    expect(lastOption().xAxis).toMatchObject({ type: "log" });
    expect(ec.init).toHaveBeenCalledTimes(1);
  });

  test("re-renders with fresh token colours when the theme changes", async () => {
    render(<ParetoChart summary={EVAL_SUMMARY} />);
    await waitFor(() => expect(ec.setOption).toHaveBeenCalled());
    const calls = ec.setOption.mock.calls.length;
    document.documentElement.style.setProperty("--chart-router-1", "#123456");
    await act(async () => {
      document.documentElement.dataset.theme = "dark";
    });
    await waitFor(() =>
      expect(ec.setOption.mock.calls.length).toBeGreaterThan(calls),
    );
    const series = lastOption().series as Array<Record<string, unknown>>;
    expect(series.find((s) => s.type === "line")).toMatchObject({
      itemStyle: { color: "#123456" },
    });
  });

  test("resizes with its container and disposes on unmount", async () => {
    const { unmount } = render(<ParetoChart summary={EVAL_SUMMARY} />);
    await waitFor(() => expect(ec.setOption).toHaveBeenCalled());
    const observer = resizeObservers.at(-1);
    if (!observer) throw new Error("no ResizeObserver");
    act(() => observer.cb([], observer as unknown as ResizeObserver));
    expect(ec.resize).toHaveBeenCalled();
    unmount();
    expect(ec.dispose).toHaveBeenCalled();
  });

  test("shows an empty state when there are no points", () => {
    render(<ParetoChart summary={{ ...EVAL_SUMMARY, pareto: [] }} />);
    expect(screen.getByText(/no cost–quality points/i)).toBeInTheDocument();
    expect(ec.init).not.toHaveBeenCalled();
  });
});

describe("EvalsView", () => {
  test("loading, then the table and chart", async () => {
    let resolve: (s: EvalSummary) => void = () => {};
    const client = stubClient(
      () =>
        new Promise<EvalSummary>((r) => {
          resolve = r;
        }),
    );
    render(
      <ApiProvider client={client}>
        <EvalsView />
      </ApiProvider>,
    );
    expect(
      screen.getAllByRole("status", { name: /loading/i }).length,
    ).toBeGreaterThan(0);
    await act(async () => resolve(structuredClone(EVAL_SUMMARY)));
    expect(
      screen.getByRole("table", { name: /holdout · n = 400/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("table", { name: /cost.*quality/i }),
    ).toBeInTheDocument();
  });

  test("an error offers a retry that refetches", async () => {
    const user = userEvent.setup();
    const load = vi
      .fn<() => Promise<EvalSummary>>()
      .mockRejectedValueOnce(new Error("down"))
      .mockResolvedValue(structuredClone(EVAL_SUMMARY));
    render(
      <ApiProvider client={stubClient(load)}>
        <EvalsView />
      </ApiProvider>,
    );
    const alert = await screen.findByRole("alert");
    await user.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("table", { name: /holdout · n = 400/ }),
    ).toBeInTheDocument();
    expect(load).toHaveBeenCalledTimes(2);
  });

  test("no systems shows an empty state", async () => {
    const empty: EvalSummary = { ...EVAL_SUMMARY, systems: [], pareto: [] };
    render(
      <ApiProvider client={stubClient(async () => empty)}>
        <EvalsView />
      </ApiProvider>,
    );
    expect(await screen.findByText(/no eval results yet/i)).toBeInTheDocument();
  });
});

describe("/evals page", () => {
  test("shows the Demo badge and the results with the mock client", async () => {
    render(
      <ApiProvider client={new MockApiClient({ delayMs: 0 })}>
        <EvalsPage />
      </ApiProvider>,
    );
    expect(
      screen.getByRole("heading", { level: 1, name: "Evals" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Demo data")).toBeInTheDocument();
    expect(
      await screen.findByRole("table", { name: /holdout · n = 400/ }),
    ).toBeInTheDocument();
  });
});
