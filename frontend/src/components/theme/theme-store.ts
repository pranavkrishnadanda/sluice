import {
  isThemeMode,
  THEME_MODES,
  THEME_STORAGE_KEY,
  type ThemeMode,
} from "./theme-script";

/**
 * A tiny external store for the theme mode. `<html data-theme>` is the single
 * source of truth (the inline head script sets it before paint), so the mode is
 * consistent whether or not a ThemeProvider is mounted.
 */

const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

export function readStoredMode(): ThemeMode {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemeMode(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function applyMode(mode: ThemeMode): void {
  document.documentElement.dataset.theme = mode;
  notify();
}

export function setThemeMode(mode: ThemeMode): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch {
    // Storage unavailable: the theme still applies for this page view.
  }
  applyMode(mode);
}

export function nextThemeMode(mode: ThemeMode): ThemeMode {
  const index = THEME_MODES.indexOf(mode);
  return THEME_MODES[(index + 1) % THEME_MODES.length] ?? "system";
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getSnapshot(): ThemeMode {
  const current = document.documentElement.dataset.theme;
  return isThemeMode(current) ? current : "system";
}

export function getServerSnapshot(): ThemeMode {
  return "system";
}
