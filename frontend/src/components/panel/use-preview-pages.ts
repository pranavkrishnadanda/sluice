"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  type DataPreview,
  PREVIEW_MAX_LIMIT,
  type PreviewRow,
} from "@/lib/api/types";
import { useResource } from "./use-resource";

/** Rows per preview request: the server's maximum page size. */
export const PREVIEW_PAGE_SIZE = PREVIEW_MAX_LIMIT;

export type PreviewPages =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; error: unknown; retry: () => void }
  | {
      status: "ready";
      /** Every page loaded so far, concatenated; `nextOffset` is the next. */
      preview: DataPreview;
      loadingMore: boolean;
      /** The last next-page request failed; `loadMore` retries it. */
      moreError: unknown;
      /** Fetches the next page, if any. Safe to call repeatedly. */
      loadMore: () => void;
    };

interface Extra {
  /** The first page these follow; a refetched first page drops them. */
  base: DataPreview;
  rows: PreviewRow[];
  nextOffset: number | null;
  loading: boolean;
  error: unknown;
}

/**
 * The run preview, paged: the first page loads with `key` (like
 * {@link useResource}); later pages load only when `loadMore` is called,
 * typically as the virtualised table nears the end of what is loaded.
 * `load`'s `signal` aborts when `key` changes or the hook unmounts.
 */
export function usePreviewPages(
  key: string | null,
  load: (
    offset: number,
    limit: number,
    signal: AbortSignal,
  ) => Promise<DataPreview>,
): PreviewPages {
  const first = useResource(key, (signal) =>
    load(0, PREVIEW_PAGE_SIZE, signal),
  );
  const [extra, setExtra] = useState<Extra | null>(null);
  // The first page a next-page request is in flight for (no double fetch).
  const inFlight = useRef<DataPreview | null>(null);
  // Aborts next-page requests made for the current key.
  const more = useRef<AbortController | null>(null);
  useEffect(() => {
    if (key === null) return;
    const controller = new AbortController();
    more.current = controller;
    return () => controller.abort();
  }, [key]);

  const base = first.status === "ready" ? first.data : null;
  const current = base && extra?.base === base ? extra : null;

  const preview = useMemo<DataPreview | null>(() => {
    if (!base) return null;
    if (!current) return base;
    return {
      ...base,
      rows: [...base.rows, ...current.rows],
      nextOffset: current.nextOffset,
    };
  }, [base, current]);

  if (first.status !== "ready") return first;
  if (!base || !preview) return { status: "loading" };

  const loadMore = () => {
    const offset = preview.nextOffset;
    if (offset == null || inFlight.current === base) return;
    inFlight.current = base;
    setExtra({
      base,
      rows: current?.rows ?? [],
      nextOffset: offset,
      loading: true,
      error: null,
    });
    const signal = more.current?.signal ?? AbortSignal.abort();
    load(offset, PREVIEW_PAGE_SIZE, signal).then(
      (page) => {
        if (inFlight.current === base) inFlight.current = null;
        setExtra((prev) =>
          prev?.base === base
            ? {
                ...prev,
                rows: [...prev.rows, ...page.rows],
                nextOffset: page.nextOffset,
                loading: false,
              }
            : prev,
        );
      },
      (error: unknown) => {
        if (inFlight.current === base) inFlight.current = null;
        if (signal.aborted) return;
        setExtra((prev) =>
          prev?.base === base ? { ...prev, loading: false, error } : prev,
        );
      },
    );
  };

  return {
    status: "ready",
    preview,
    loadingMore: current?.loading ?? false,
    moreError: current?.error ?? null,
    loadMore,
  };
}
