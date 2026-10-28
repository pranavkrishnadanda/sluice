"use client";

import type { EChartsType } from "echarts/core";
import { useEffect, useMemo, useRef, useState } from "react";
import { useMediaQuery } from "@/components/shell/use-media-query";
import type { EvalSummary } from "@/lib/api/types";
import { formatPct, formatUsd } from "@/lib/format";
import { useChartColours } from "./chart-colours";
import { buildParetoOption, costAxisBounds, paretoRows } from "./pareto-option";

export interface ParetoChartProps {
  summary: EvalSummary;
}

function PointsTable({
  summary,
  visible,
}: {
  summary: EvalSummary;
  visible: boolean;
}) {
  const rows = paretoRows(summary);
  // The sr-only wrapper is a 1px clipped box; on the <table> itself the
  // table would still size to its content and overflow the scroll parent.
  return (
    <div className={visible ? "mt-3 overflow-x-auto" : "sr-only"}>
      <table className="w-full text-left text-sm tabular-nums [&_td]:px-2 [&_td]:py-1.5 [&_th]:px-2 [&_th]:py-1.5">
        <caption className={visible ? "pb-2 text-left text-muted text-xs" : ""}>
          Cost and quality per point
        </caption>
        <thead>
          <tr>
            <th scope="col">System</th>
            <th scope="col">Kind</th>
            <th scope="col">λ</th>
            <th scope="col">Cost per 1k rows</th>
            <th scope="col">Quality</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.system}|${r.lambda ?? ""}|${r.costPer1kRowsUsd}`}>
              <th scope="row">{r.system}</th>
              <td>{r.kind}</td>
              <td>{r.lambda ?? "—"}</td>
              <td>{formatUsd(r.costPer1kRowsUsd)}</td>
              <td>{formatPct(r.quality)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Cost per 1k rows (log x) against quality (y): baselines as labelled
 * diamonds, each router as a line through its λ settings (λ in the
 * tooltip). ECharts loads on demand in its own chunk; colours come from the
 * CSS tokens and follow the theme; the chart tracks its box with a
 * ResizeObserver. The plot is hidden from assistive tech, which gets a
 * table of every point instead (shown visibly if the chart fails to load).
 */
export function ParetoChart({ summary }: ParetoChartProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [chart, setChart] = useState<EChartsType | null>(null);
  const [failed, setFailed] = useState(false);
  const colours = useChartColours();
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const rows = useMemo(() => paretoRows(summary), [summary]);
  const empty = rows.length === 0;
  const hasZero = useMemo(() => costAxisBounds(rows).hasZero, [rows]);

  useEffect(() => {
    const host = hostRef.current;
    if (empty || !host) return;
    let live = true;
    let instance: EChartsType | null = null;
    let observer: ResizeObserver | null = null;
    import("./echarts").then(
      ({ init }) => {
        if (!live) return;
        instance = init(host, null, { renderer: "svg" });
        if (typeof ResizeObserver === "function") {
          observer = new ResizeObserver(() => instance?.resize());
          observer.observe(host);
        }
        setChart(instance);
      },
      () => {
        if (live) setFailed(true);
      },
    );
    return () => {
      live = false;
      observer?.disconnect();
      instance?.dispose();
      setChart(null);
    };
  }, [empty]);

  useEffect(() => {
    const host = hostRef.current;
    if (!chart || !host) return;
    chart.setOption(
      buildParetoOption(summary, colours, {
        reducedMotion,
        fontFamily: getComputedStyle(host).fontFamily,
      }),
      { notMerge: true },
    );
  }, [chart, summary, colours, reducedMotion]);

  if (empty) {
    return (
      <div className="grid min-h-40 place-items-center rounded-xl bg-chip px-4 py-6 text-center">
        <p className="text-muted text-sm">
          No cost–quality points in this summary yet.
        </p>
      </div>
    );
  }

  return (
    <div>
      <div className="relative h-72 w-full min-w-0 overflow-hidden sm:h-80">
        <div ref={hostRef} aria-hidden="true" className="absolute inset-0" />
        {!chart && !failed && (
          <output
            aria-label="Loading chart"
            className="absolute inset-0 grid place-items-center"
          >
            <span className="block size-full animate-pulse rounded-xl bg-chip" />
          </output>
        )}
        {failed && (
          <p
            role="alert"
            className="absolute inset-0 grid place-items-center rounded-xl bg-chip px-4 text-center text-sm"
          >
            <span>
              <span className="font-medium text-error">
                Couldn’t load the chart.
              </span>{" "}
              <span className="text-muted">
                Every point is listed in the table below.
              </span>
            </span>
          </p>
        )}
      </div>
      <p className="mt-2 text-muted text-xs leading-5">
        Log cost scale.
        {hasZero && " Zero-cost systems sit on the left edge, marked $0."} Hover
        or tap a point for its exact values.
      </p>
      <PointsTable summary={summary} visible={failed} />
    </div>
  );
}
