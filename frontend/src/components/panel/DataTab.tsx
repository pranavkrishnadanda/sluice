"use client";
/* oxlint-disable jsx-a11y/no-redundant-roles, jsx-a11y/no-interactive-element-to-noninteractive-role */
// ^ The virtualised <table> is laid out with block/grid display, which can
// strip implicit table semantics in some browsers (notably Safari), so its
// table/rowgroup/row/cell roles are restated on purpose.

import {
  useVirtualizer,
  type VirtualizerOptions,
} from "@tanstack/react-virtual";
import { useId, useMemo, useRef, useState } from "react";
import type { DataPreview, PreviewCell } from "@/lib/api/types";
import { formatCount } from "@/lib/format";

/** One table row per preview cell: the long `pk | column | before | after` view. */
interface CellRow {
  pk: string;
  column: string;
  cell: PreviewCell;
  changed: boolean;
}

const ROW_HEIGHT = 36;
const OVERSCAN = 8;
const GRID =
  "grid grid-cols-[2.75rem_6.25rem_minmax(5rem,1fr)_minmax(5rem,1fr)]";

const isChanged = (c: PreviewCell) => c.flagged || c.before !== c.after;

function flatten(preview: DataPreview): CellRow[] {
  const out: CellRow[] = [];
  for (const row of preview.rows) {
    for (const column of preview.columns) {
      const cell = row.cells[column];
      if (cell)
        out.push({ pk: row.pk, column, cell, changed: isChanged(cell) });
    }
  }
  return out;
}

type ObserveRect = VirtualizerOptions<
  HTMLTableElement,
  Element
>["observeElementRect"];

/** A fixed viewport for environments without layout (tests). */
function fixedRect(height: number): ObserveRect {
  return (_instance, cb) => {
    cb({ width: 360, height });
    return undefined;
  };
}

function Value({ value }: { value: string | null }) {
  return value === null ? <span className="italic">null</span> : value;
}

function BeforeCell({ row }: { row: CellRow }) {
  const { before } = row.cell;
  if (!row.changed) return <Value value={before} />;
  return (
    <del className="text-muted decoration-1">
      <Value value={before} />
    </del>
  );
}

function AfterCell({ row }: { row: CellRow }) {
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <span className={`truncate ${row.changed ? "font-medium" : ""}`}>
        <Value value={row.cell.after} />
      </span>
      {row.cell.flagged && (
        <span className="shrink-0 rounded-full bg-chip px-1.5 font-sans font-medium text-[0.6875rem] text-warn leading-[1.125rem]">
          flagged
        </span>
      )}
    </span>
  );
}

const title = (v: string | null) => (v === null ? "null" : v);

export interface DataTabProps {
  preview: DataPreview;
  /** Fixed viewport height in px; tests only (jsdom has no layout). */
  viewportHeight?: number;
}

/**
 * Before/after preview as a virtualised `pk | column | before | after`
 * table: one row per cell, so a 48k-row preview is ~145k table rows of
 * which only the visible window (plus overscan) is in the DOM. ARIA table
 * semantics with `aria-rowcount` / `aria-rowindex` keep the full size
 * discoverable; the scroll box is focusable for keyboard scrolling.
 */
export function DataTab({ preview, viewportHeight }: DataTabProps) {
  const [changedOnly, setChangedOnly] = useState(true);
  const scrollRef = useRef<HTMLTableElement>(null);
  const captionId = useId();

  const all = useMemo(() => flatten(preview), [preview]);
  const stats = useMemo(() => {
    let changed = 0;
    let flagged = 0;
    for (const r of all) {
      if (r.changed) changed++;
      if (r.cell.flagged) flagged++;
    }
    return { changed, flagged };
  }, [all]);
  const rows = useMemo(
    () => (changedOnly ? all.filter((r) => r.changed) : all),
    [all, changedOnly],
  );

  // TanStack Virtual returns a mutable instance, so the React Compiler (if
  // enabled) skips memoising this component; it re-renders on scroll anyway.
  // oxlint-disable-next-line react/incompatible-library
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: OVERSCAN,
    ...(viewportHeight === undefined
      ? {}
      : {
          initialRect: { width: 360, height: viewportHeight },
          observeElementRect: fixedRect(viewportHeight),
        }),
  });

  const partial = preview.rows.length < preview.totalRows;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <p id={captionId} className="text-muted text-xs leading-5">
          <span className="font-medium text-fg">
            {formatCount(stats.changed)}
          </span>{" "}
          changed
          {stats.flagged > 0 && (
            <>
              {" · "}
              <span className="font-medium text-warn">
                {formatCount(stats.flagged)}
              </span>{" "}
              flagged
            </>
          )}
          {" · "}
          {partial
            ? `first ${formatCount(preview.rows.length)} of ${formatCount(preview.totalRows)} rows`
            : `${formatCount(preview.totalRows)} rows`}
        </p>
        <button
          type="button"
          role="switch"
          aria-checked={changedOnly}
          onClick={() => setChangedOnly((v) => !v)}
          className="group inline-flex min-h-8 items-center gap-2 rounded-full px-1 font-medium text-fg text-xs"
        >
          <span
            aria-hidden="true"
            className="relative h-4.5 w-8 rounded-full bg-chip shadow-[inset_0_0_0_1px_var(--text-muted)] transition-colors duration-150 group-aria-checked:bg-accent group-aria-checked:shadow-none"
          >
            <span className="absolute top-0.5 left-0.5 size-3.5 rounded-full bg-glass-solid shadow-sm transition-transform duration-150 group-aria-checked:translate-x-3.5" />
          </span>
          Changed only
        </button>
      </div>

      {rows.length === 0 ? (
        <p className="grid min-h-40 place-items-center rounded-xl bg-chip px-4 text-center text-muted text-sm">
          {changedOnly
            ? "No changed cells — every value passed through as is."
            : "No rows in this preview."}
        </p>
      ) : (
        // A real <table> laid out as a grid so rows can be absolutely
        // positioned. `display` overrides can strip implicit table semantics
        // in some browsers, so the roles are restated explicitly.
        // biome-ignore-start lint/a11y/noRedundantRoles: explicit roles survive the grid/block display overrides.
        <table
          ref={scrollRef}
          role="table"
          aria-label="Before and after values"
          aria-describedby={captionId}
          aria-rowcount={rows.length + 1}
          /* oxlint-disable jsx-a11y/no-noninteractive-tabindex */
          // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must be keyboard-focusable (WCAG 2.1.1).
          tabIndex={0}
          /* oxlint-enable jsx-a11y/no-noninteractive-tabindex */
          className="relative block min-h-72 flex-1 overflow-auto overscroll-contain rounded-xl font-mono text-[0.6875rem] ring-1 ring-black/10 ring-inset dark:ring-white/10"
        >
          <thead role="rowgroup" className="sticky top-0 z-10 grid">
            <tr
              role="row"
              aria-rowindex={1}
              className={`${GRID} min-w-78 border-black/10 border-b bg-glass-solid font-sans font-medium text-muted dark:border-white/10`}
            >
              {["pk", "column", "before", "after"].map((h) => (
                <th
                  key={h}
                  role="columnheader"
                  scope="col"
                  className="px-2 py-2 text-left font-medium"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody
            role="rowgroup"
            className="relative grid min-w-78"
            style={{ height: virtualizer.getTotalSize() }}
          >
            {virtualizer.getVirtualItems().map((item) => {
              const row = rows[item.index];
              if (!row) return null;
              return (
                <tr
                  key={item.key}
                  role="row"
                  aria-rowindex={item.index + 2}
                  className={`${GRID} absolute inset-x-0 top-0 items-center border-black/5 border-b dark:border-white/5`}
                  style={{
                    height: ROW_HEIGHT,
                    transform: `translateY(${item.start}px)`,
                  }}
                >
                  <td role="cell" className="truncate px-2 text-muted">
                    {row.pk}
                  </td>
                  <td role="cell" className="truncate px-2" title={row.column}>
                    {row.column}
                  </td>
                  <td
                    role="cell"
                    className="truncate px-2"
                    title={title(row.cell.before)}
                  >
                    <BeforeCell row={row} />
                  </td>
                  <td
                    role="cell"
                    className="min-w-0 px-2"
                    title={title(row.cell.after)}
                  >
                    <AfterCell row={row} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        // biome-ignore-end lint/a11y/noRedundantRoles: end of the virtualised table.
      )}
    </div>
  );
}

/** Placeholder while the preview loads: the table's frame, no fake values. */
export function DataTabSkeleton() {
  return (
    <div aria-hidden="true" className="flex flex-1 flex-col gap-3">
      <div className="flex items-center justify-between">
        <span className="h-4 w-40 animate-pulse rounded-full bg-chip" />
        <span className="h-4 w-24 animate-pulse rounded-full bg-chip" />
      </div>
      <div className="min-h-72 flex-1 space-y-2 rounded-xl p-3 ring-1 ring-black/10 ring-inset dark:ring-white/10">
        {Array.from({ length: 8 }, (_, i) => (
          <span
            // biome-ignore lint/suspicious/noArrayIndexKey: static placeholder rows.
            key={i}
            className="block h-5 animate-pulse rounded-md bg-chip"
          />
        ))}
      </div>
    </div>
  );
}
