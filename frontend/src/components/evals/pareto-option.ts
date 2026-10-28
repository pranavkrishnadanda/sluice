import type { LineSeriesOption, ScatterSeriesOption } from "echarts/charts";
import type {
  GridComponentOption,
  LegendComponentOption,
  TooltipComponentOption,
} from "echarts/components";
import type { ComposeOption } from "echarts/core";
import type { EvalSummary } from "@/lib/api/types";
import { formatPct, formatUsd } from "@/lib/format";
import type { ChartColours } from "./chart-colours";

/*
 * Pure option builder for the cost–quality (Pareto) chart. Type-only ECharts
 * imports: nothing here pulls ECharts into a bundle.
 */

export type ParetoOption = ComposeOption<
  | ScatterSeriesOption
  | LineSeriesOption
  | GridComponentOption
  | TooltipComponentOption
  | LegendComponentOption
>;

/** One plotted point, flattened from `summary.pareto`. */
export interface ParetoRow {
  system: string;
  kind: "baseline" | "router";
  costPer1kRowsUsd: number;
  quality: number;
  lambda: number | null;
}

/** A data item: `value` is where it is drawn, `cost` the measured cost. */
interface ParetoDatum {
  name: string;
  value: [number, number];
  cost: number;
  lambda?: number | null;
}

export const BASELINES_SERIES = "Baselines";

export function paretoRows(summary: EvalSummary): ParetoRow[] {
  return summary.pareto.flatMap((s) =>
    s.points.map((p) => ({
      system: s.system,
      kind: s.kind,
      costPer1kRowsUsd: p.costPer1kRowsUsd,
      quality: p.quality,
      lambda: p.lambda ?? null,
    })),
  );
}

/**
 * Log-axis bounds: whole decades around the positive costs. A log axis has
 * no zero, so when zero-cost points exist they get a decade of their own
 * below the cheapest paid point's decade: `min` is then only ever their draw
 * position (labelled "$0"), never a real cost tick.
 */
export function costAxisBounds(rows: readonly ParetoRow[]): {
  min: number;
  max: number;
  hasZero: boolean;
} {
  const costs = rows.map((r) => r.costPer1kRowsUsd);
  const positive = costs.filter((c) => c > 0);
  const hasZero = costs.some((c) => c <= 0);
  if (positive.length === 0) return { min: 0.01, max: 1, hasZero };
  const lo = Math.min(...positive);
  const hi = Math.max(...positive);
  const minExp = Math.floor(Math.log10(lo)) - (hasZero ? 1 : 0);
  const maxExp = Math.max(Math.ceil(Math.log10(hi)), minExp + 1);
  // Round away float noise (10 ** -2 is 0.01 exactly, but be explicit).
  const pow = (e: number) => Number((10 ** e).toPrecision(1));
  return { min: pow(minExp), max: pow(maxExp), hasZero };
}

/** Quality-axis bounds snapped to 5-point steps with a little headroom. */
function qualityAxisBounds(rows: readonly ParetoRow[]): {
  min: number;
  max: number;
} {
  const qs = rows.map((r) => r.quality);
  const lo = Math.min(...qs);
  const hi = Math.max(...qs);
  const step = 0.05;
  const min = Math.max(0, Math.floor((lo - 0.02) / step) * step);
  const max = Math.min(1, Math.ceil((hi + 0.01) / step) * step);
  return { min: Number(min.toFixed(2)), max: Number(max.toFixed(2)) };
}

const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};
const escapeHtml = (s: string) =>
  s.replace(/[&<>"']/g, (c) => HTML_ESCAPES[c] ?? c);

function isDatum(v: unknown): v is ParetoDatum {
  if (typeof v !== "object" || v === null) return false;
  const d = v as Partial<ParetoDatum>;
  return (
    typeof d.name === "string" &&
    typeof d.cost === "number" &&
    Array.isArray(d.value)
  );
}

/** Tooltip HTML for one point (ECharts renders it as HTML: escape names). */
function tooltipHtml(params: unknown): string {
  const p = (Array.isArray(params) ? params[0] : params) as
    | { data?: unknown }
    | undefined;
  const d = p?.data;
  if (!isDatum(d)) return "";
  const lines = [
    `<strong>${escapeHtml(d.name)}</strong>`,
    `Cost per 1k rows: ${formatUsd(d.cost)}`,
    `Quality: ${formatPct(d.value[1])}`,
  ];
  if (d.lambda != null) lines.push(`λ = ${d.lambda}`);
  return lines.join("<br/>");
}

export interface ParetoOptionSettings {
  reducedMotion?: boolean;
  fontFamily?: string;
}

export function buildParetoOption(
  summary: EvalSummary,
  colours: ChartColours,
  { reducedMotion = false, fontFamily }: ParetoOptionSettings = {},
): ParetoOption {
  const rows = paretoRows(summary);
  const x = costAxisBounds(rows);
  const y = qualityAxisBounds(rows);
  const logSpan = Math.log10(x.max) - Math.log10(x.min);
  const drawAt = (cost: number) => (cost > 0 ? cost : x.min);
  const isZeroPosition = (v: number) =>
    Math.abs(Math.log10(v) - Math.log10(x.min)) < 1e-9;
  /**
   * Labels sit right of a point, except near the dear end of the axis where
   * they drop below it (clear of router lines, which rise from the left).
   */
  const labelSide = (cost: number) =>
    (Math.log10(drawAt(cost)) - Math.log10(x.min)) / logSpan > 0.6
      ? "bottom"
      : "right";

  const baselineData = summary.pareto
    .filter((s) => s.kind === "baseline")
    .flatMap((s) =>
      s.points.map(
        (p): ParetoDatum & { label: { position: "bottom" | "right" } } => ({
          name: s.system,
          value: [drawAt(p.costPer1kRowsUsd), p.quality],
          cost: p.costPer1kRowsUsd,
          label: { position: labelSide(p.costPer1kRowsUsd) },
        }),
      ),
    );

  const axisCommon = {
    nameTextStyle: { color: colours.muted, fontSize: 12 },
    axisLine: { lineStyle: { color: colours.grid } },
    axisTick: { lineStyle: { color: colours.grid } },
    axisLabel: { color: colours.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: colours.grid } },
  };

  const series: Array<ScatterSeriesOption | LineSeriesOption> = [];
  if (baselineData.length > 0) {
    series.push({
      type: "scatter",
      name: BASELINES_SERIES,
      symbol: "diamond",
      symbolSize: 13,
      itemStyle: {
        color: colours.baseline,
        borderColor: colours.surface,
        borderWidth: 2,
      },
      label: {
        show: true,
        formatter: "{b}",
        color: colours.text,
        fontSize: 12,
        distance: 8,
      },
      labelLayout: { hideOverlap: true },
      emphasis: { scale: 1.25 },
      data: baselineData,
    });
  }

  summary.pareto
    .filter((s) => s.kind === "router")
    .forEach((s, i) => {
      const colour = colours.routers[Math.min(i, colours.routers.length - 1)];
      const data = [...s.points]
        .sort((a, b) => a.costPer1kRowsUsd - b.costPer1kRowsUsd)
        .map((p, j): ParetoDatum & { label?: { show: boolean } } => ({
          name: s.system,
          value: [drawAt(p.costPer1kRowsUsd), p.quality],
          cost: p.costPer1kRowsUsd,
          ...(p.lambda != null ? { lambda: p.lambda } : {}),
          // Direct-label the line once, at its cheapest end.
          ...(j === 0 ? { label: { show: true } } : {}),
        }));
      series.push({
        type: "line",
        name: s.system,
        symbol: "circle",
        symbolSize: 9,
        showSymbol: true,
        lineStyle: { width: 2, color: colour },
        itemStyle: {
          color: colour,
          borderColor: colours.surface,
          borderWidth: 2,
        },
        label: {
          show: false,
          position: "bottom",
          formatter: "{a}",
          color: colours.text,
          fontSize: 12,
          distance: 8,
        },
        emphasis: { scale: 1.25 },
        data,
      });
    });

  return {
    animation: !reducedMotion,
    animationDuration: 300,
    textStyle: fontFamily ? { fontFamily } : {},
    grid: {
      left: 8,
      right: 24,
      top: 40,
      bottom: 8,
      outerBoundsMode: "same",
      outerBoundsContain: "all",
    },
    legend: {
      top: 0,
      left: 0,
      itemWidth: 14,
      itemHeight: 10,
      itemGap: 16,
      textStyle: { color: colours.text, fontSize: 12 },
      inactiveColor: colours.grid,
    },
    tooltip: {
      trigger: "item",
      confine: true,
      backgroundColor: colours.surface,
      borderColor: colours.grid,
      borderWidth: 1,
      padding: [8, 10],
      textStyle: { color: colours.text, fontSize: 12 },
      extraCssText: "border-radius:10px;box-shadow:0 8px 24px rgb(0 0 0/0.18);",
      formatter: tooltipHtml,
    },
    xAxis: {
      ...axisCommon,
      type: "log",
      logBase: 10,
      min: x.min,
      max: x.max,
      name: "Cost per 1k rows (log)",
      nameLocation: "middle",
      nameGap: 28,
      axisLabel: {
        ...axisCommon.axisLabel,
        // Only the zero-cost draw position reads "$0"; real ticks keep
        // their values (it sits a decade below every paid cost).
        formatter: (v: number) =>
          x.hasZero && isZeroPosition(v) ? "$0" : formatUsd(v),
      },
    },
    yAxis: {
      ...axisCommon,
      type: "value",
      min: y.min,
      max: y.max,
      interval: 0.05,
      name: "Quality",
      nameLocation: "end",
      nameGap: 12,
      axisLabel: {
        ...axisCommon.axisLabel,
        formatter: (v: number) => `${Math.round(v * 100)}%`,
      },
    },
    series,
  };
}
