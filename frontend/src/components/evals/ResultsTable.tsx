import type { CI, EvalSummary } from "@/lib/api/types";
import { type CIUnit, formatCIParts } from "@/lib/format";

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

/**
 * Mean over its bracketed interval, on two lines so seven columns fit the
 * card; the text still reads "86.1% [84.2, 87.9]" as one string. The mean
 * never breaks; the interval may wrap after its comma when a column is
 * narrow (or the platform's fonts run wide), instead of overflowing.
 */
function Interval({ ci, unit }: { ci: CI | null; unit?: CIUnit }) {
  if (!ci) return <NotMeasured />;
  const { mean, interval } = formatCIParts(ci, unit);
  return (
    <span className="block">
      <span className="block whitespace-nowrap font-medium text-fg">
        {mean}
      </span>{" "}
      <span className="block text-[0.6875rem] text-muted">
        <IntervalText interval={interval} />
      </span>
    </span>
  );
}

/**
 * Metric columns with their share of the table width. The table uses
 * `table-layout: fixed`, so these shares, not the text, set the columns:
 * the fit no longer depends on each platform's exact glyph widths (CI's
 * Linux fonts once pushed the auto layout 9px past the card). Latency and
 * cost get more room for their longer intervals ("[940 ms, 1.3 s]").
 */
/**
 * "[940 ms, 1.3 s]" that can only break after its comma, never inside a
 * bound ("245" / "ms]"); the text content is unchanged.
 */
function IntervalText({ interval }: { interval: string }) {
  const [lo, hi] = interval.split(", ");
  if (hi === undefined) return <>{interval}</>;
  return (
    <>
      <span className="whitespace-nowrap">{`${lo},`}</span>{" "}
      <span className="whitespace-nowrap">{hi}</span>
    </>
  );
}

const COLUMNS: ReadonlyArray<{ label: string; hint: string; width: string }> = [
  { label: "Cell accuracy", hint: "higher is better", width: "14%" },
  { label: "Fabrication rate", hint: "lower is better", width: "14%" },
  { label: "Contract pass rate", hint: "higher is better", width: "14%" },
  { label: "pass^4", hint: "higher is better", width: "14%" },
  { label: "p95 latency", hint: "lower is better", width: "18%" },
  { label: "Cost / 1k rows", hint: "lower is better", width: "16%" },
];
/** System names wrap at their hyphens ("always-" / "strong"). */
const SYSTEM_WIDTH = "10%";

// `break-words`: last resort if a word still can't fit its fixed column.
const TH = "px-1.5 py-2.5 text-left align-bottom font-medium break-words";
const TD = "px-1.5 py-2.5 align-top font-mono text-xs tabular-nums break-words";

function Row({ row }: { row: SystemRow }) {
  return (
    <tr className="border-fg/8 border-t transition-colors duration-150 hover:bg-chip">
      <th
        scope="row"
        // Names may wrap at their hyphens ("always-" / "strong").
        className={`${TD} text-left font-medium text-fg`}
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
        {/* min-w: below ~40rem the card scrolls sideways instead of
            squeezing seven columns into unreadable slivers. */}
        <table className="w-full min-w-[40rem] table-fixed border-collapse text-sm">
          <caption className="pb-2 text-left font-medium text-muted text-xs uppercase tracking-[0.08em]">
            {`${summary.split} · n = ${summary.n}`}
          </caption>
          <colgroup>
            <col style={{ width: SYSTEM_WIDTH }} />
            {COLUMNS.map((c) => (
              <col key={c.label} style={{ width: c.width }} />
            ))}
          </colgroup>
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
        Brackets show 95% confidence intervals. pass^4 is the estimated
        probability that all 4 runs of a case succeed (τ-bench pass^k
        estimator). — means not measured.
      </p>
    </div>
  );
}
