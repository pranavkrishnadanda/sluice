"use client";

import { useCallback, useSyncExternalStore } from "react";
import type { ThemeMode } from "./theme-script";
import {
  getServerSnapshot,
  getSnapshot,
  nextThemeMode,
  setThemeMode,
  subscribe,
} from "./theme-store";

export interface UseThemeResult {
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
  cycleMode: () => void;
}

export function useTheme(): UseThemeResult {
  const mode = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  const cycleMode = useCallback(() => {
    setThemeMode(nextThemeMode(getSnapshot()));
  }, []);
  return { mode, setMode: setThemeMode, cycleMode };
}
