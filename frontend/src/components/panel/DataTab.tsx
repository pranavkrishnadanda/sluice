"use client";

import {
  useVirtualizer,
  type VirtualizerOptions,
} from "@tanstack/react-virtual";
import { type Key, useEffect, useId, useMemo, useRef, useState } from "react";
import type { DataPreview, PreviewCell } from "@/lib/api/types";
import { formatCount } from "@/lib/format";

/** One table row per preview cell: the long `pk | column | before | after` view. */
interface CellRow {
  pk: string;
  column: string;
  cell: PreviewCell;
  changed: boolean;
  /** The cell's whole row was quarantined. */
  quarantined: boolean;
  reason: string | null;
}

/** Fixed data-row height in px (the virtualiser does not measure rows). */
export const DATA_ROW_HEIGHT = 36;
const OVERSCAN = 8;
/** Request the next page when the window is this close to the loaded end. */
const LOOKAHEAD = 50;
/** Column widths; `table-fixed` keeps them stable as rows scroll in and out. */
const COLUMNS = ["3.5rem", "5.75rem", "auto", "auto"] as const;
const CELL = "truncate border-black/5 border-b px-1.5 dark:border-white/5";

const isChanged = (c: PreviewCell) => c.flagged || c.before !== c.after;

function flatten(preview: DataPreview): CellRow[] {
  const out: CellRow[] = [];
  for (const row of preview.rows) {
    for (const column of preview.columns) {
      const cell = row.cells[column];
      if (!cell) continue;
      out.push({
        pk: row.pk,
        column,
        cell,
        // A quarantined row is never "unchanged": it was held back.
        changed: row.quarantined || isChanged(cell),
        quarantined: row.quarantined,
        reason: row.reason,
      });
    }
  }
  return out;
}

type ObserveRect = VirtualizerOptions<
  HTMLElement,
  Element
>["observeElementRect"];

/** A fixed viewport for environments without layout (tests). */
export function fixedRect(height: number): ObserveRect {
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

const PILL =
  "shrink-0 rounded-full bg-chip px-1.5 font-sans font-medium text-[0.6875rem] text-warn leading-[1.125rem]";

function AfterCell({ row }: { row: CellRow }) {
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <span className={`truncate ${row.changed ? "font-medium" : ""}`}>
        <Value value={row.cell.after} />
      </span>
      {row.cell.flagged && <span className={PILL}>flagged</span>}
      {row.quarantined && (
        <span className={PILL} title={row.reason ?? undefined}>
          quarantined
          {row.reason && <span className="sr-only">: {row.reason}</span>}
        </span>
      )}
    </span>
  );
}

const title = (v: string | null) => (v === null ? "null" : v);

export interface DataTabProps {
  /** Every page loaded so far; `nextOffset` non-null means more exist. */
  preview: DataPreview;
  /** A next-page request is in flight. */
  loadingMore?: boolean;
  /** The last next-page request failed (a Retry is offered). */
  moreError?: unknown;
  /** Asks for the next page as the window nears the loaded end. */
  onLoadMore?: () => void;
  /** Fixed viewport height in px; tests only (jsdom has no layout). */
  viewportHeight?: number;
  /**
   * The rows come from a demo fixture replayed for this run, not from the
   * user's file; the caption says so.
   */
  sample?: boolean;
}

/** "1,000 of 48,210 rows loaded", or with `sample`, "Sample run · … sample rows". */
function rowsCaption(
  loaded: number,
  total: number,
  partial: boolean,
  sample: boolean,
): string {
  const rows = sample ? "sample rows" : "rows";
  const count = partial
    ? `${formatCount(loaded)} of ${formatCount(total)} ${rows}${sample ? "" : " loaded"}`
    : `${formatCount(total)} ${rows}`;
  return sample ? `Sample run · ${count}` : count;
}

/**
 * Before/after preview as a virtualised `pk | column | before | after`
 * table: one row per cell, so a 48k-row preview is ~145k table rows of
 * which only the visible window (plus overscan) is in the DOM. ARIA table
 * semantics with `aria-rowcount` / `aria-rowindex` keep the full size
 * discoverable; the scroll box is focusable for keyboard scrolling.
 *
 * The preview arrives in pages: when the window comes within
 * {@link LOOKAHEAD} rows of the last loaded row (or a filter leaves nothing
 * to show), `onLoadMore` asks for the next one. Until the last page is in,
 * the row count is unknown (`aria-rowcount="-1"`).
 */
export function DataTab({
  preview,
  loadingMore = false,
  moreError = null,
  onLoadMore,
  viewportHeight,
  sample = false,
}: DataTabProps) {
  const [changedOnly, setChangedOnly] = useState(true);
  const scrollRef = useRef<HTMLElement>(null);
  const captionId = useId();

  const all = useMemo(() => flatten(preview), [preview]);
  const stats = useMemo(() => {
    let changed = 0;
    let flagged = 0;
    for (const r of all) {
      if (r.changed) changed++;
      if (r.cell.flagged) flagged++;
    }
    const quarantined = preview.rows.filter((r) => r.quarantined).length;
    return { changed, flagged, quarantined };
  }, [all, preview.rows]);
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
    estimateSize: () => DATA_ROW_HEIGHT,
    overscan: OVERSCAN,
    ...(viewportHeight === undefined
      ? {}
      : {
          initialRect: { width: 360, height: viewportHeight },
          observeElementRect: fixedRect(viewportHeight),
        }),
  });

  const hasMore = preview.nextOffset != null;
  const partial = hasMore || preview.rows.length < preview.totalRows;
  const items = virtualizer.getVirtualItems();
  const lastIndex = items.at(-1)?.index ?? -1;
  const wantMore =
    hasMore &&
    !loadingMore &&
    moreError == null &&
    lastIndex >= rows.length - 1 - LOOKAHEAD;
  useEffect(() => {
    if (wantMore) onLoadMore?.();
  }, [wantMore, onLoadMore]);

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
          {stats.quarantined > 0 && (
            <>
              {" · "}
              <span className="font-medium text-warn">
                {formatCount(stats.quarantined)}
              </span>{" "}
              quarantined
            </>
          )}
          {" · "}
          {rowsCaption(preview.rows.length, preview.totalRows, partial, sample)}
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

      {rows.length === 0 && !hasMore ? (
        <p className="grid min-h-40 place-items-center rounded-xl bg-chip px-4 text-center text-muted text-sm">
          {changedOnly
            ? "No changed cells — every value passed through as is."
            : "No rows in this preview."}
        </p>
      ) : (
        // Native table, virtualised with spacer rows above and below the
        // visible window: no display overrides, so the implicit table
        // semantics hold in every browser and no explicit roles are needed.
        // A named <section> is a region landmark: keyboard focus lands on
        // "Before and after values, scrollable" rather than a bare <div>.
        <section
          ref={scrollRef}
          aria-label="Before and after values, scrollable"
          /* oxlint-disable jsx-a11y/no-noninteractive-tabindex */
          // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must be keyboard-focusable (WCAG 2.1.1).
          tabIndex={0}
          /* oxlint-enable jsx-a11y/no-noninteractive-tabindex */
          className="min-h-72 flex-1 overflow-auto overscroll-contain rounded-xl ring-1 ring-black/10 ring-inset dark:ring-white/10"
        >
          <VirtualRows
            rows={rows}
            items={items}
            totalSize={virtualizer.getTotalSize()}
            captionId={captionId}
            complete={!hasMore}
          />
        </section>
      )}
      {(loadingMore || moreError != null) && (
        <MoreStatus failed={moreError != null} onRetry={onLoadMore} />
      )}
    </div>
  );
}

interface WindowItem {
  key: Key;
  index: number;
  start: number;
  end: number;
}

/** Below the table: "Loading more rows…" or a failed page with Retry. */
export function MoreStatus({
  failed,
  onRetry,
}: {
  failed: boolean;
  onRetry?: () => void;
}) {
  if (!failed) {
    return <output className="text-muted text-xs">Loading more rows…</output>;
  }
  return (
    <div role="alert" className="flex items-center gap-3 text-xs">
      <span className="text-error">Couldn’t load more rows.</span>
      <button
        type="button"
        onClick={onRetry}
        className="min-h-8 rounded-full bg-chip px-3 font-medium text-fg transition-colors duration-150 hover:bg-raised"
      >
        Retry
      </button>
    </div>
  );
}

function VirtualRows({
  rows,
  items,
  totalSize,
  captionId,
  complete,
}: {
  rows: readonly CellRow[];
  items: readonly WindowItem[];
  totalSize: number;
  captionId: string;
  /** Every page is loaded, so the row count is final. */
  complete: boolean;
}) {
  const top = items[0]?.start ?? 0;
  const bottom = Math.max(0, totalSize - (items.at(-1)?.end ?? 0));
  return (
    // The off-window rows are stood in for by the table's own top/bottom
    // margin, not spacer rows, so assistive tech never meets empty rows.
    <table
      style={{ marginTop: top, marginBottom: bottom }}
      aria-label="Before and after values"
      aria-describedby={captionId}
      aria-rowcount={complete ? rows.length + 1 : -1}
      className="w-full min-w-78 table-fixed border-separate border-spacing-0 font-mono text-[0.6875rem]"
    >
      <colgroup>
        {COLUMNS.map((width, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: fixed, positional columns.
          <col key={i} style={width === "auto" ? undefined : { width }} />
        ))}
      </colgroup>
      <thead>
        <tr aria-rowindex={1}>
          {["pk", "column", "before", "after"].map((h) => (
            <th
              key={h}
              scope="col"
              className="sticky top-0 z-10 border-black/10 border-b bg-glass-solid px-1.5 py-2 text-left font-medium font-sans text-muted dark:border-white/10"
            >
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {items.map((item) => {
          const row = rows[item.index];
          if (!row) return null;
          return (
            <tr
              key={item.key}
              aria-rowindex={item.index + 2}
              style={{ height: DATA_ROW_HEIGHT }}
            >
              <td className={`${CELL} text-muted`} title={row.pk}>
                {row.pk}
              </td>
              <td className={CELL} title={row.column}>
                {row.column}
              </td>
              <td className={CELL} title={title(row.cell.before)}>
                <BeforeCell row={row} />
              </td>
              <td className={CELL} title={title(row.cell.after)}>
                <AfterCell row={row} />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
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
