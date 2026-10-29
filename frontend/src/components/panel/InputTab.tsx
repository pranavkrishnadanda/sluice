"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useId, useRef } from "react";
import type { InputPreview } from "@/lib/api/types";
import { formatCount } from "@/lib/format";
import { DATA_ROW_HEIGHT, fixedRect, MoreStatus } from "./DataTab";

const OVERSCAN = 8;
/** Request the next page when the window is this close to the loaded end. */
const LOOKAHEAD = 50;
const CELL =
  "max-w-56 truncate border-black/5 border-b px-2 dark:border-white/5";

export interface InputTabProps {
  /** Every page of the raw upload loaded so far. */
  input: InputPreview;
  loadingMore?: boolean;
  moreError?: unknown;
  onLoadMore?: () => void;
  /** The run finished without loading anything. */
  neverLoaded?: boolean;
  /** The rows are a demo fixture's, not the user's file. */
  sample?: boolean;
  /** Fixed viewport height in px; tests only (jsdom has no layout). */
  viewportHeight?: number;
}

function caption(loaded: number, total: number, sample: boolean): string {
  const rows = sample ? "sample rows" : "rows";
  const count =
    loaded < total
      ? `${formatCount(loaded)} of ${formatCount(total)} ${rows}`
      : `${formatCount(total)} ${rows}`;
  return sample ? `Sample run · ${count}` : count;
}

/**
 * The uploaded file as it arrived, before anything was repaired or loaded:
 * one virtualised table row per file row, the file's own columns, every
 * value as raw text. Wide files scroll sideways inside the box. Shown until
 * the run loads (then the before/after preview takes over), and for runs
 * that never load, so a refused file can still be inspected.
 */
export function InputTab({
  input,
  loadingMore = false,
  moreError = null,
  onLoadMore,
  neverLoaded = false,
  sample = false,
  viewportHeight,
}: InputTabProps) {
  const scrollRef = useRef<HTMLElement>(null);
  const headingId = useId();
  const captionId = useId();

  // oxlint-disable-next-line react/incompatible-library
  const virtualizer = useVirtualizer({
    count: input.rows.length,
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

  const hasMore = input.nextOffset != null;
  const items = virtualizer.getVirtualItems();
  const lastIndex = items.at(-1)?.index ?? -1;
  const wantMore =
    hasMore &&
    !loadingMore &&
    moreError == null &&
    lastIndex >= input.rows.length - 1 - LOOKAHEAD;
  useEffect(() => {
    if (wantMore) onLoadMore?.();
  }, [wantMore, onLoadMore]);

  const top = items[0]?.start ?? 0;
  const bottom = Math.max(
    0,
    virtualizer.getTotalSize() - (items.at(-1)?.end ?? 0),
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="space-y-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h2 id={headingId} className="font-medium text-sm leading-5">
            Uploaded file (not loaded)
          </h2>
          <span className="rounded-full bg-chip px-2 font-medium text-[0.6875rem] text-muted leading-[1.125rem]">
            raw
          </span>
        </div>
        <p id={captionId} className="text-pretty text-muted text-xs leading-5">
          {caption(input.rows.length, input.totalRows, sample)}
          {" · "}
          {neverLoaded
            ? "This run didn’t load any data, so there is no before/after preview."
            : "The before/after preview appears once the data has loaded."}
        </p>
      </div>

      {input.rows.length === 0 && !hasMore ? (
        <p className="grid min-h-40 place-items-center rounded-xl bg-chip px-4 text-center text-muted text-sm">
          The uploaded file has no rows.
        </p>
      ) : (
        <section
          ref={scrollRef}
          aria-label="Uploaded file, scrollable"
          /* oxlint-disable jsx-a11y/no-noninteractive-tabindex */
          // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must be keyboard-focusable (WCAG 2.1.1).
          tabIndex={0}
          /* oxlint-enable jsx-a11y/no-noninteractive-tabindex */
          className="min-h-72 flex-1 overflow-auto overscroll-contain rounded-xl ring-1 ring-black/10 ring-inset dark:ring-white/10"
        >
          <table
            style={{ marginTop: top, marginBottom: bottom }}
            aria-label="Uploaded file"
            aria-describedby={captionId}
            aria-rowcount={hasMore ? -1 : input.rows.length + 1}
            className="min-w-full border-separate border-spacing-0 font-mono text-[0.6875rem]"
          >
            <thead>
              <tr aria-rowindex={1}>
                {["#", ...input.columns].map((h, i) => (
                  <th
                    // biome-ignore lint/suspicious/noArrayIndexKey: a file may repeat a column name; position is the identity.
                    key={i}
                    scope="col"
                    className="sticky top-0 z-10 whitespace-nowrap border-black/10 border-b bg-glass-solid px-2 py-2 text-left font-medium font-sans text-muted dark:border-white/10"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const row = input.rows[item.index];
                if (!row) return null;
                return (
                  <tr
                    key={item.key}
                    aria-rowindex={item.index + 2}
                    style={{ height: DATA_ROW_HEIGHT }}
                  >
                    <td className={`${CELL} text-muted tabular-nums`}>
                      {item.index + 1}
                    </td>
                    {input.columns.map((_, c) => {
                      const value = row[c] ?? null;
                      return (
                        <td
                          // biome-ignore lint/suspicious/noArrayIndexKey: cells align with positional columns.
                          key={c}
                          className={CELL}
                          title={value ?? "null"}
                        >
                          {value === null ? (
                            <span className="text-muted italic">null</span>
                          ) : (
                            value
                          )}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      )}
      {(loadingMore || moreError != null) && (
        <MoreStatus failed={moreError != null} onRetry={onLoadMore} />
      )}
    </div>
  );
}
