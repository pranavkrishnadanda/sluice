import type { CI, EvalSummary } from "@/lib/api/types";
import { formatCI } from "@/lib/format";

type SystemRow = EvalSummary["systems"][number];

/** "—" for a metric that was not measured, with words for screen readers. */
function NotMeasured() {
  return (
    <>
      <span aria-hidden="true">—</span>
      <span className="sr-only">not measured</span>
    </>
  );
}

function Interval({ ci, unit }: { ci: CI | null; unit?: "usd" | "ms" }) {
  if (!ci) return <NotMeasured />;
  return <span className="whitespace-nowrap">{formatCI(ci, unit)}</span>;
}

const COLUMNS: ReadonlyArray<{ label: string; hint: string }> = [
  { label: "Cell accuracy", hint: "higher is better" },
  { label: "Fabrication rate", hint: "lower is better" },
  { label: "Contract pass rate", hint: "higher is better" },
  { label: "pass^4", hint: "higher is better" },
  { label: "p95 latency", hint: "lower is better" },
  { label: "Cost / 1k rows", hint: "lower is better" },
];

const TH = "whitespace-nowrap px-2 py-2.5 text-left align-bottom font-medium";
const TD = "px-2 py-3 font-mono text-xs tabular-nums";

function Row({ row }: { row: SystemRow }) {
  return (
    <tr className="border-fg/8 border-t transition-colors duration-150 hover:bg-chip">
      <th
        scope="row"
        className={`${TD} whitespace-nowrap text-left font-medium text-fg`}
      >
        {row.system}
      </th>
      <td className={TD}>
        <Interval ci={row.cellAccuracy} />
      </td>
      <td className={TD}>
        <Interval ci={row.fabricationRate} />
      </td>
      <td className={TD}>
        <Interval ci={row.contractPassRate} />
      </td>
      <td className={TD}>
        <Interval ci={row.passHat4} />
      </td>
      <td className={TD}>
        <Interval ci={row.p95LatencyMs} unit="ms" />
      </td>
      <td className={TD}>
        <Interval ci={row.costPer1kRowsUsd} unit="usd" />
      </td>
    </tr>
  );
}

export interface ResultsTableProps {
  summary: EvalSummary;
}

/**
 * One row per evaluated system with each metric's mean and 95% interval,
 * exactly as `formatCI` prints it. Scrolls sideways inside its own box on
 * narrow screens; the page itself never does.
 */
export function ResultsTable({ summary }: ResultsTableProps) {
  if (summary.systems.length === 0) {
    return (
      <div className="grid min-h-32 place-items-center rounded-xl bg-chip px-4 py-6 text-center">
        <p className="text-sm">
          <span className="font-medium">No eval results yet.</span>{" "}
          <span className="text-muted">
            Results appear here once an eval run has been scored.
          </span>
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <section
        aria-label="Results by system, scrollable"
        /* oxlint-disable jsx-a11y/no-noninteractive-tabindex */
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must be keyboard-focusable (WCAG 2.1.1).
        tabIndex={0}
        /* oxlint-enable jsx-a11y/no-noninteractive-tabindex */
        // `relative`: the sr-only "not measured" spans are absolutely
        // positioned and would otherwise escape this scroller's clipping.
        className="relative -mx-1 overflow-x-auto rounded-xl px-1 focus-visible:outline-offset-0"
      >
        <table className="w-full min-w-[56rem] border-collapse text-sm">
          <caption className="pb-2 text-left font-medium text-muted text-xs uppercase tracking-[0.08em]">
            {`${summary.split} · n = ${summary.n}`}
          </caption>
          <thead>
            <tr className="text-muted text-xs">
              <th scope="col" className={TH}>
                System
              </th>
              {COLUMNS.map((c) => (
                <th key={c.label} scope="col" className={TH}>
                  <span className="block text-fg">{c.label}</span>
                  <span className="block font-normal">{c.hint}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {summary.systems.map((row) => (
              <Row key={row.system} row={row} />
            ))}
          </tbody>
        </table>
      </section>
      <p className="text-muted text-xs leading-5">
        Brackets show 95% confidence intervals. pass^4 is the estimated chance
        that all 4 runs of a case succeed (τ-bench pass^k estimator). — means
        not measured.
      </p>
    </div>
  );
}
