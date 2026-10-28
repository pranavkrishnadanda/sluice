"use client";

import type { ReactNode } from "react";
import type { ThemeMode } from "./theme-script";
import { useTheme } from "./use-theme";

const ICON_PATHS: Record<ThemeMode, ReactNode> = {
  system: (
    <>
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <path d="M8 20h8M12 16v4" />
    </>
  ),
  light: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </>
  ),
  dark: <path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11Z" />,
};

export function ThemeToggle({ className }: { className?: string }) {
  const { mode, cycleMode } = useTheme();
  const label = `Theme: ${mode}`;

  return (
    <button
      type="button"
      onClick={cycleMode}
      aria-label={label}
      title={label}
      className={["glass glass-pill theme-toggle", className]
        .filter(Boolean)
        .join(" ")}
    >
      <svg
        aria-hidden="true"
        focusable="false"
        viewBox="0 0 24 24"
        width="18"
        height="18"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {ICON_PATHS[mode]}
      </svg>
    </button>
  );
}
