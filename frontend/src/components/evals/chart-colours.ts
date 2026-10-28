"use client";

import { useMemo, useSyncExternalStore } from "react";

/**
 * Colours the cost–quality chart paints with, read from the CSS tokens in
 * globals.css at runtime so the chart follows the theme. Series tokens are
 * contrast-checked in tests/design/contrast.test.ts (CHART_SERIES_TOKENS).
 */
export interface ChartColours {
  /** Every baseline point (they are told apart by direct labels). */
  baseline: string;
  /** Router lines, in order; a third router reuses the last slot. */
  routers: readonly [string, string];
  text: string;
  muted: string;
  grid: string;
  /** Opaque surface for the tooltip. */
  surface: string;
}

const TOKENS = {
  baseline: "--chart-baseline",
  router1: "--chart-router-1",
  router2: "--chart-router-2",
  text: "--text",
  muted: "--text-muted",
  grid: "--chart-grid",
  surface: "--glass-solid",
} as const;

/** Light-theme values; used only where tokens can't be read (no CSS). */
export const FALLBACK_COLOURS: ChartColours = {
  baseline: "#007658",
  routers: ["#2a63d6", "#ad4514"],
  text: "#1d1d1f",
  muted: "#4d4d55",
  grid: "rgb(29 29 31 / 0.1)",
  surface: "#f2f4fb",
};

function readColours(): ChartColours {
  const style = getComputedStyle(document.documentElement);
  const read = (token: string, fallback: string) =>
    style.getPropertyValue(token).trim() || fallback;
  const f = FALLBACK_COLOURS;
  return {
    baseline: read(TOKENS.baseline, f.baseline),
    routers: [
      read(TOKENS.router1, f.routers[0]),
      read(TOKENS.router2, f.routers[1]),
    ],
    text: read(TOKENS.text, f.text),
    muted: read(TOKENS.muted, f.muted),
    grid: read(TOKENS.grid, f.grid),
    surface: read(TOKENS.surface, f.surface),
  };
}

const DARK_QUERY = "(prefers-color-scheme: dark)";

/**
 * Fires when the resolved theme can have changed: the `data-theme`
 * attribute on `<html>` (theme toggle) or the OS scheme ("system" mode).
 */
function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
  const mql =
    typeof window.matchMedia === "function"
      ? window.matchMedia(DARK_QUERY)
      : null;
  mql?.addEventListener?.("change", onChange);
  return () => {
    observer.disconnect();
    mql?.removeEventListener?.("change", onChange);
  };
}

/* A string snapshot is stable across reads, as useSyncExternalStore needs. */
const getSnapshot = () => JSON.stringify(readColours());
const getServerSnapshot = () => JSON.stringify(FALLBACK_COLOURS);

/** Current chart colours; a new object only when a resolved token changes. */
export function useChartColours(): ChartColours {
  const key = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  return useMemo(() => JSON.parse(key) as ChartColours, [key]);
}
