"use client";

import { type ReactNode, useEffect } from "react";
import {
  isThemeMode,
  REDUCED_TRANSPARENCY_QUERY,
  THEME_STORAGE_KEY,
} from "./theme-script";
import { applyMode, readStoredMode } from "./theme-store";

function syncReducedTransparency(matches: boolean): void {
  const html = document.documentElement;
  if (matches) html.dataset.reduceTransparency = "";
  else delete html.dataset.reduceTransparency;
}

/**
 * Keeps `<html>` in sync with the stored theme and the OS transparency
 * preference. The inline head script handles first paint; this covers client
 * navigation, other tabs changing the theme, and live preference changes.
 * Renders no wrapper element.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  useEffect(() => {
    applyMode(readStoredMode());

    function onStorage(event: StorageEvent): void {
      if (event.key !== THEME_STORAGE_KEY) return;
      applyMode(isThemeMode(event.newValue) ? event.newValue : "system");
    }
    window.addEventListener("storage", onStorage);

    let query: MediaQueryList | undefined;
    function onTransparencyChange(event: MediaQueryListEvent): void {
      syncReducedTransparency(event.matches);
    }
    try {
      query = window.matchMedia(REDUCED_TRANSPARENCY_QUERY);
      syncReducedTransparency(query.matches);
      query.addEventListener("change", onTransparencyChange);
    } catch {
      // matchMedia unsupported: CSS media queries still apply.
    }

    return () => {
      window.removeEventListener("storage", onStorage);
      query?.removeEventListener("change", onTransparencyChange);
    };
  }, []);

  return children;
}
